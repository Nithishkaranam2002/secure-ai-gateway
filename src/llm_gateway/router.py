"""Resilient routing between the primary and backup providers.

Two failures are worth moving for: a 429, and silence past the timeout budget.
Everything else is either the caller's fault or unrecoverable, and retrying it
elsewhere doubles the cost and the latency for no chance of success.

Failover is a single hop. Primary, then backup, then stop. A gateway that keeps
retrying under load makes an outage worse rather than better.
"""

import asyncio
from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Any

from src.core.audit import record
from src.core.config import settings
from src.core.errors import GatewayError
from src.core.logging_setup import get_logger
from src.llm_gateway.providers import (
    Provider,
    ProviderRateLimited,
    ProviderRequestRejected,
    ProviderTimeout,
    ProviderUnavailable,
    build_backup,
    build_primary,
)

logger = get_logger(__name__)

COMPONENT = "llm_gateway"

# The two conditions the brief names, plus 5xx, which is the same situation as a
# timeout from the caller's point of view: the provider cannot serve this now.
FAILOVER_ON = (ProviderRateLimited, ProviderTimeout, ProviderUnavailable)


@dataclass
class RouteResult:
    body: dict[str, Any]
    provider_name: str
    model: str
    failed_over: bool
    total_tokens: int


class ModelRouter:
    def __init__(
        self,
        primary: Provider | None = None,
        backup: Provider | None = None,
        timeout_ms: int | None = None,
    ) -> None:
        self.primary = primary or build_primary()
        self.backup = backup or build_backup()
        self.timeout_seconds = (timeout_ms or settings.upstream_timeout_ms) / 1000

    # ------------------------------------------------------------ non streaming

    async def complete(
        self, request: dict[str, Any], actor: str | None = None
    ) -> RouteResult:
        try:
            body = await self._call_with_deadline(self.primary, request)
            return self._result(body, self.primary, failed_over=False)

        except ProviderRequestRejected as exc:
            # The caller's request is wrong. The backup would reject it the same
            # way, so failing over would only cost time and money.
            record(
                COMPONENT,
                "complete",
                "rejected",
                actor=actor,
                detail={"status": exc.status_code},
            )
            raise GatewayError(
                status_code=400,
                code="invalid_request",
                message="The request was rejected by the model provider.",
                internal_detail=str(exc),
            ) from exc

        except FAILOVER_ON as exc:
            reason = type(exc).__name__
            logger.warning("primary failed with %s, failing over", reason)
            record(
                COMPONENT,
                "complete",
                "failover",
                actor=actor,
                detail={"reason": reason},
            )
            try:
                body = await self._call_with_deadline(self.backup, request)
                return self._result(body, self.backup, failed_over=True)
            except Exception as backup_exc:
                # Both providers are gone. One flat message outward; the two
                # real causes stay in the log under the error id.
                record(
                    COMPONENT,
                    "complete",
                    "both_failed",
                    actor=actor,
                    detail={
                        "primary": reason,
                        "backup": type(backup_exc).__name__,
                    },
                )
                raise GatewayError(
                    status_code=503,
                    code="no_provider_available",
                    message="No model provider is currently available.",
                    internal_detail=f"primary={exc!r} backup={backup_exc!r}",
                ) from backup_exc

    async def _call_with_deadline(
        self, provider: Provider, request: dict[str, Any]
    ) -> dict[str, Any]:
        """Run the call against a clock and cancel it if the clock wins.

        asyncio.timeout cancels the task it wraps, which closes the underlying
        connection. Without that cancellation the abandoned request would stay
        open, and enough of those exhaust the connection pool.
        """
        try:
            async with asyncio.timeout(self.timeout_seconds):
                return await provider.complete(request)
        except asyncio.TimeoutError as exc:
            raise ProviderTimeout(
                f"{provider.config.name} exceeded {self.timeout_seconds:.1f}s"
            ) from exc

    def _result(
        self, body: dict[str, Any], provider: Provider, failed_over: bool
    ) -> RouteResult:
        usage = body.get("usage") or {}
        return RouteResult(
            body=body,
            provider_name=provider.config.name,
            model=provider.config.model,
            failed_over=failed_over,
            total_tokens=int(usage.get("total_tokens") or 0),
        )

    # ---------------------------------------------------------------- streaming

    async def stream(
        self, request: dict[str, Any], actor: str | None = None
    ) -> tuple[Provider, AsyncIterator[str], bool]:
        """Open a stream, failing over only while opening it.

        The deadline covers reaching the first chunk and nothing after it. Once
        text has been sent to the user, switching providers would splice two
        different answers together, so a mid stream failure is reported rather
        than retried.
        """
        try:
            iterator = await self._open_stream(self.primary, request)
            return self.primary, iterator, False

        except ProviderRequestRejected as exc:
            record(
                COMPONENT,
                "stream",
                "rejected",
                actor=actor,
                detail={"status": exc.status_code},
            )
            raise GatewayError(
                status_code=400,
                code="invalid_request",
                message="The request was rejected by the model provider.",
                internal_detail=str(exc),
            ) from exc

        except FAILOVER_ON as exc:
            reason = type(exc).__name__
            logger.warning("primary stream failed with %s, failing over", reason)
            record(
                COMPONENT,
                "stream",
                "failover",
                actor=actor,
                detail={"reason": reason},
            )
            try:
                iterator = await self._open_stream(self.backup, request)
                return self.backup, iterator, True
            except Exception as backup_exc:
                record(
                    COMPONENT,
                    "stream",
                    "both_failed",
                    actor=actor,
                    detail={
                        "primary": reason,
                        "backup": type(backup_exc).__name__,
                    },
                )
                raise GatewayError(
                    status_code=503,
                    code="no_provider_available",
                    message="No model provider is currently available.",
                    internal_detail=f"primary={exc!r} backup={backup_exc!r}",
                ) from backup_exc

    async def _open_stream(
        self, provider: Provider, request: dict[str, Any]
    ) -> AsyncIterator[str]:
        """Pull the first chunk under the deadline, then hand back the rest.

        The first chunk is held and replayed by the wrapper below, so the caller
        receives a complete stream while the deadline applied only to opening it.
        """
        source = provider.stream(request)
        try:
            async with asyncio.timeout(self.timeout_seconds):
                first = await source.__anext__()
        except asyncio.TimeoutError as exc:
            await source.aclose()
            raise ProviderTimeout(
                f"{provider.config.name} sent no chunk within "
                f"{self.timeout_seconds:.1f}s"
            ) from exc
        except StopAsyncIteration:
            # An empty but valid stream. Not a failure to route around.
            async def empty() -> AsyncIterator[str]:
                return
                yield  # pragma: no cover

            return empty()
        except Exception:
            await source.aclose()
            raise

        async def replayed() -> AsyncIterator[str]:
            yield first
            async for piece in source:
                yield piece

        return replayed()


router = ModelRouter()

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
from src.core.errors import GatewayError, sanitise
from src.core.logging_setup import get_logger
from src.llm_gateway.circuit_breaker import CircuitBreaker
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
        # Tracked for the primary only. The backup is the last resort, so it is
        # always attempted: skipping it would mean failing a request that might
        # still have succeeded.
        self.primary_breaker = CircuitBreaker("primary")

    # ------------------------------------------------------------ non streaming

    async def complete(
        self, request: dict[str, Any], actor: str | None = None
    ) -> RouteResult:
        if not self.primary_breaker.allows_request():
            # The primary is known to be down, so skip the timeout entirely
            # rather than paying it again for every request during an outage.
            record(
                COMPONENT,
                "complete",
                "circuit_open",
                actor=actor,
                detail={"provider": "primary"},
            )
            body = await self._call_with_deadline(self.backup, request)
            return self._result(body, self.backup, failed_over=True)

        try:
            body = await self._call_with_deadline(self.primary, request)
            self.primary_breaker.record_success()
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
            self.primary_breaker.record_failure()
            logger.warning("primary failed with %s, failing over", reason)
            record(
                COMPONENT,
                "complete",
                "failover",
                actor=actor,
                detail={
                    "reason": reason,
                    "circuit": self.primary_breaker.status().state.value,
                },
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

        except GatewayError:
            raise

        except Exception as exc:
            # Anything not anticipated above. Sanitising only the failures we
            # predicted is not enough: an unexpected exception carries text this
            # project never wrote and never reviewed, which is exactly how a key
            # or a file path ends up in a response. The catch has to be total and
            # it has to be here, at the boundary.
            error = sanitise(exc)
            record(
                COMPONENT,
                "complete",
                "internal_error",
                actor=actor,
                detail={"error_id": error.error_id, "type": type(exc).__name__},
                event_id=error.error_id,
            )
            logger.exception("unexpected routing failure error_id=%s", error.error_id)
            raise error from exc

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
        if not self.primary_breaker.allows_request():
            record(
                COMPONENT,
                "stream",
                "circuit_open",
                actor=actor,
                detail={"provider": "primary"},
            )
            iterator = await self._open_stream(self.backup, request)
            return self.backup, iterator, True

        try:
            iterator = await self._open_stream(self.primary, request)
            self.primary_breaker.record_success()
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
            self.primary_breaker.record_failure()
            logger.warning("primary stream failed with %s, failing over", reason)
            record(
                COMPONENT,
                "stream",
                "failover",
                actor=actor,
                detail={
                    "reason": reason,
                    "circuit": self.primary_breaker.status().state.value,
                },
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

        except GatewayError:
            raise

        except Exception as exc:
            error = sanitise(exc)
            record(
                COMPONENT,
                "stream",
                "internal_error",
                actor=actor,
                detail={"error_id": error.error_id, "type": type(exc).__name__},
                event_id=error.error_id,
            )
            logger.exception("unexpected stream routing failure error_id=%s", error.error_id)
            raise error from exc

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

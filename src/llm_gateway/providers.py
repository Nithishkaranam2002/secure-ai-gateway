"""Outbound calls to the model providers.

This is the only module in the project that touches httpx. Everything above it
asks for "the primary" or "the backup" and never learns which company is behind
either name.

The SDKs are deliberately not used. They retry a 429 internally, which would
hide the exact signal the router in router.py needs in order to fail over. They
apply timeouts per attempt rather than per call, so a three second budget can
take much longer. And they unpack the SSE stream that this gateway has to
forward on intact.

Groq accepts the same request shape as OpenAI, so one class serves both with a
different base URL, key and model name.
"""

from dataclasses import dataclass
from collections.abc import AsyncIterator
from typing import Any

import httpx

from src.core.config import settings
from src.core.logging_setup import get_logger

logger = get_logger(__name__)

CHAT_COMPLETIONS_PATH = "/chat/completions"
SSE_DATA_PREFIX = "data: "
SSE_DONE = "[DONE]"


class ProviderTimeout(Exception):
    """The provider did not respond inside the budget."""


class ProviderRateLimited(Exception):
    """The provider returned 429."""


class ProviderUnavailable(Exception):
    """The provider returned a 5xx or the connection failed."""


class ProviderRequestRejected(Exception):
    """The provider returned a 4xx that is not a rate limit.

    Kept separate because it must not trigger a failover. A malformed request
    will be rejected identically by the backup, so retrying elsewhere doubles
    the cost and the latency for no chance of success.
    """

    def __init__(self, status_code: int, message: str) -> None:
        super().__init__(message)
        self.status_code = status_code


@dataclass(frozen=True)
class ProviderConfig:
    name: str
    base_url: str
    api_key: str
    model: str

    @property
    def is_configured(self) -> bool:
        return bool(self.api_key)


class Provider:
    def __init__(self, config: ProviderConfig, timeout_ms: int) -> None:
        self.config = config
        self.timeout_seconds = timeout_ms / 1000

    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self.config.api_key}",
            "Content-Type": "application/json",
        }

    def _timeout(self) -> httpx.Timeout:
        """Bound the silence, not the length of the answer.

        connect and read carry the budget, because what we are protecting
        against is a provider that does not respond. The overall timeout stays
        unbounded on purpose: a healthy streaming response legitimately stays
        open far longer than the budget, and capping the total would truncate
        long answers mid sentence.
        """
        return httpx.Timeout(
            connect=self.timeout_seconds,
            read=self.timeout_seconds,
            write=self.timeout_seconds,
            pool=self.timeout_seconds,
        )

    def _payload(self, request: dict[str, Any], stream: bool) -> dict[str, Any]:
        payload = dict(request)
        payload["model"] = self.config.model
        payload["stream"] = stream
        if stream:
            # Ask the provider to report usage in the final chunk, so token
            # accounting works on streamed calls too where supported.
            payload["stream_options"] = {"include_usage": True}
        return payload

    def _raise_for_status(self, status_code: int, body: str) -> None:
        if status_code == 429:
            raise ProviderRateLimited(f"{self.config.name} returned 429")
        if status_code >= 500:
            raise ProviderUnavailable(
                f"{self.config.name} returned {status_code}"
            )
        if status_code >= 400:
            raise ProviderRequestRejected(status_code, body[:500])

    async def complete(self, request: dict[str, Any]) -> dict[str, Any]:
        """Non streaming call. Returns the parsed body including usage."""
        url = self.config.base_url.rstrip("/") + CHAT_COMPLETIONS_PATH
        try:
            async with httpx.AsyncClient(timeout=self._timeout()) as client:
                response = await client.post(
                    url, headers=self._headers(), json=self._payload(request, False)
                )
        except httpx.TimeoutException as exc:
            raise ProviderTimeout(f"{self.config.name} timed out") from exc
        except httpx.HTTPError as exc:
            raise ProviderUnavailable(
                f"{self.config.name} connection failed: {type(exc).__name__}"
            ) from exc

        self._raise_for_status(response.status_code, response.text)
        return response.json()

    async def stream(self, request: dict[str, Any]) -> AsyncIterator[str]:
        """Streaming call. Yields the text content of each delta as it arrives.

        The response is never accumulated. Each line is parsed, its content
        pulled out and handed on, and then dropped.
        """
        url = self.config.base_url.rstrip("/") + CHAT_COMPLETIONS_PATH
        try:
            async with httpx.AsyncClient(timeout=self._timeout()) as client:
                async with client.stream(
                    "POST",
                    url,
                    headers=self._headers(),
                    json=self._payload(request, True),
                ) as response:
                    if response.status_code >= 400:
                        body = (await response.aread()).decode("utf-8", "replace")
                        self._raise_for_status(response.status_code, body)

                    async for line in response.aiter_lines():
                        text = _content_from_sse_line(line)
                        if text:
                            yield text
        except httpx.TimeoutException as exc:
            raise ProviderTimeout(f"{self.config.name} timed out") from exc
        except httpx.HTTPError as exc:
            raise ProviderUnavailable(
                f"{self.config.name} connection failed: {type(exc).__name__}"
            ) from exc


def _content_from_sse_line(line: str) -> str:
    """Pull the text out of one server sent event line, if it carries any."""
    if not line or not line.startswith(SSE_DATA_PREFIX):
        return ""
    body = line[len(SSE_DATA_PREFIX) :].strip()
    if not body or body == SSE_DONE:
        return ""
    try:
        import json

        payload = json.loads(body)
    except ValueError:
        logger.warning("provider sent a data line that is not JSON")
        return ""
    choices = payload.get("choices") or []
    if not choices:
        return ""
    delta = choices[0].get("delta") or {}
    content = delta.get("content")
    return content if isinstance(content, str) else ""


def build_primary() -> Provider:
    return Provider(
        ProviderConfig(
            name="primary",
            base_url=settings.primary_base_url,
            api_key=settings.openai_api_key,
            model=settings.primary_model,
        ),
        settings.upstream_timeout_ms,
    )


def build_backup() -> Provider:
    return Provider(
        ProviderConfig(
            name="backup",
            base_url=settings.backup_base_url,
            api_key=settings.groq_api_key,
            model=settings.backup_model,
        ),
        settings.upstream_timeout_ms,
    )

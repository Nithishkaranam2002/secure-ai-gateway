"""Providers that fail on demand.

A real provider cannot be made to return 429 on request, and cannot be made to
hang for exactly the timeout budget. Without something that can, the failover
and timeout paths in the router are untestable, and an untested failover path is
one that has never run.

These live only under tests/ and are never imported by anything in src/.
"""

import asyncio
from collections.abc import AsyncIterator
from typing import Any

from src.llm_gateway.providers import (
    ProviderConfig,
    ProviderRateLimited,
    ProviderRequestRejected,
    ProviderUnavailable,
)


class ScriptedProvider:
    """Behaves however the test tells it to, and records what it was asked."""

    def __init__(
        self,
        name: str = "scripted",
        model: str = "test-model",
        raises: Exception | None = None,
        delay_seconds: float = 0.0,
        text: str = "hello from the scripted provider",
        chunks: list[str] | None = None,
        total_tokens: int = 42,
    ) -> None:
        self.config = ProviderConfig(
            name=name, base_url="http://scripted.invalid/v1", api_key="test", model=model
        )
        self.raises = raises
        self.delay_seconds = delay_seconds
        self.text = text
        self.chunks = chunks
        self.total_tokens = total_tokens
        self.complete_calls = 0
        self.stream_calls = 0
        self.cancelled = False

    async def complete(self, request: dict[str, Any]) -> dict[str, Any]:
        self.complete_calls += 1
        if self.delay_seconds:
            try:
                await asyncio.sleep(self.delay_seconds)
            except asyncio.CancelledError:
                # Records that the router really cancelled the abandoned call
                # rather than leaving it running in the background.
                self.cancelled = True
                raise
        if self.raises is not None:
            raise self.raises
        return {
            "id": "chatcmpl-scripted",
            "choices": [
                {
                    "index": 0,
                    "message": {"role": "assistant", "content": self.text},
                    "finish_reason": "stop",
                }
            ],
            "usage": {"total_tokens": self.total_tokens},
        }

    async def stream(self, request: dict[str, Any]) -> AsyncIterator[str]:
        self.stream_calls += 1
        if self.delay_seconds:
            try:
                await asyncio.sleep(self.delay_seconds)
            except asyncio.CancelledError:
                self.cancelled = True
                raise
        if self.raises is not None:
            raise self.raises
        for piece in self.chunks if self.chunks is not None else [self.text]:
            yield piece


def rate_limited(name: str = "primary") -> ScriptedProvider:
    return ScriptedProvider(name=name, raises=ProviderRateLimited(f"{name} returned 429"))


def unavailable(name: str = "primary") -> ScriptedProvider:
    return ScriptedProvider(name=name, raises=ProviderUnavailable(f"{name} returned 503"))


def rejects_request(name: str = "primary") -> ScriptedProvider:
    return ScriptedProvider(
        name=name,
        raises=ProviderRequestRejected(400, "model does not exist: internal-model-v7"),
    )


def hangs(name: str = "primary", seconds: float = 30.0) -> ScriptedProvider:
    return ScriptedProvider(name=name, delay_seconds=seconds)


def healthy(name: str = "backup", text: str = "backup answered") -> ScriptedProvider:
    return ScriptedProvider(name=name, model=f"{name}-model", text=text)

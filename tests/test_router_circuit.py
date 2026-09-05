"""The circuit breaker as the router actually uses it."""

import time

import pytest

from src.llm_gateway.circuit_breaker import State
from src.llm_gateway.router import ModelRouter
from tests.fault_injection.providers import healthy, rate_limited, hangs

REQUEST = {"messages": [{"role": "user", "content": "hello"}], "max_tokens": 30}


class TestTheRouterOpensTheCircuit:
    async def test_repeated_failures_open_it(self) -> None:
        primary, backup = rate_limited(), healthy()
        router = ModelRouter(primary=primary, backup=backup, timeout_ms=300)

        for _ in range(3):
            await router.complete(REQUEST)

        assert router.primary_breaker.status().state is State.OPEN

    async def test_once_open_the_primary_is_not_called_again(self) -> None:
        """The saving this exists for."""
        primary, backup = rate_limited(), healthy()
        router = ModelRouter(primary=primary, backup=backup, timeout_ms=300)

        for _ in range(3):
            await router.complete(REQUEST)
        calls_when_opened = primary.complete_calls

        for _ in range(5):
            result = await router.complete(REQUEST)
            assert result.provider_name == "backup"

        assert primary.complete_calls == calls_when_opened

    async def test_an_open_circuit_skips_the_timeout_entirely(self) -> None:
        """During an outage a request must not wait the full budget again."""
        primary = hangs(seconds=30.0)
        router = ModelRouter(primary=primary, backup=healthy(), timeout_ms=500)

        for _ in range(3):
            await router.complete(REQUEST)

        started = time.perf_counter()
        await router.complete(REQUEST)
        elapsed = time.perf_counter() - started

        # Straight to the backup, so nowhere near the 500 ms budget.
        assert elapsed < 0.2

    async def test_a_healthy_primary_keeps_the_circuit_closed(self) -> None:
        router = ModelRouter(primary=healthy("primary"), backup=healthy(), timeout_ms=300)
        for _ in range(5):
            await router.complete(REQUEST)
        assert router.primary_breaker.status().state is State.CLOSED

    async def test_the_circuit_recovers_when_the_primary_does(self) -> None:
        primary, backup = rate_limited(), healthy()
        router = ModelRouter(primary=primary, backup=backup, timeout_ms=300)

        for _ in range(3):
            await router.complete(REQUEST)
        assert router.primary_breaker.status().state is State.OPEN

        # The provider comes back, and the cooldown expires.
        primary.raises = None
        router.primary_breaker.cooldown_seconds = 0.0

        result = await router.complete(REQUEST)
        assert result.provider_name == "primary"
        assert router.primary_breaker.status().state is State.CLOSED

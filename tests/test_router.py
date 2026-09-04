"""Tests for failover, timeouts and error sanitisation."""

import asyncio
import time

import pytest

from src.core.errors import GatewayError
from src.llm_gateway.router import ModelRouter
from tests.fault_injection.providers import (
    ScriptedProvider,
    hangs,
    healthy,
    rate_limited,
    rejects_request,
    unavailable,
)

REQUEST = {"messages": [{"role": "user", "content": "hello"}], "max_tokens": 30}
TIMEOUT_MS = 300


def build(primary: ScriptedProvider, backup: ScriptedProvider) -> ModelRouter:
    return ModelRouter(primary=primary, backup=backup, timeout_ms=TIMEOUT_MS)


class TestHappyPath:
    async def test_primary_serves_when_healthy(self) -> None:
        primary, backup = healthy("primary", "primary answered"), healthy("backup")
        result = await build(primary, backup).complete(REQUEST)
        assert result.provider_name == "primary"
        assert result.failed_over is False
        assert backup.complete_calls == 0

    async def test_token_count_is_read_from_usage(self) -> None:
        primary = ScriptedProvider(name="primary", total_tokens=137)
        result = await build(primary, healthy()).complete(REQUEST)
        assert result.total_tokens == 137


class TestFailover:
    async def test_429_fails_over(self) -> None:
        primary, backup = rate_limited(), healthy()
        result = await build(primary, backup).complete(REQUEST)
        assert result.failed_over is True
        assert result.provider_name == "backup"
        assert backup.complete_calls == 1

    async def test_5xx_fails_over(self) -> None:
        result = await build(unavailable(), healthy()).complete(REQUEST)
        assert result.failed_over is True

    async def test_timeout_fails_over(self) -> None:
        primary = hangs(seconds=30.0)
        result = await build(primary, healthy()).complete(REQUEST)
        assert result.failed_over is True
        assert result.provider_name == "backup"

    async def test_the_abandoned_request_is_cancelled(self) -> None:
        """Not merely abandoned.

        A timed out request that is left running holds its connection open.
        Enough of those exhaust the pool, so the router must cancel rather than
        walk away.
        """
        primary = hangs(seconds=30.0)
        await build(primary, healthy()).complete(REQUEST)
        await asyncio.sleep(0)
        assert primary.cancelled is True

    async def test_failover_happens_near_the_deadline(self) -> None:
        primary = hangs(seconds=30.0)
        started = time.perf_counter()
        await build(primary, healthy()).complete(REQUEST)
        elapsed = time.perf_counter() - started
        # Waits for the budget, and does not wait for the hanging provider.
        assert TIMEOUT_MS / 1000 <= elapsed < 2.0


class TestFailuresThatMustNotFailOver:
    async def test_a_400_is_not_retried_elsewhere(self) -> None:
        """The backup would reject it identically.

        Failing over on a malformed request doubles the cost and the latency
        with no chance of a different outcome.
        """
        primary, backup = rejects_request(), healthy()
        with pytest.raises(GatewayError) as excinfo:
            await build(primary, backup).complete(REQUEST)
        assert excinfo.value.status_code == 400
        assert backup.complete_calls == 0


class TestBothProvidersDown:
    async def test_one_flat_error_when_neither_answers(self) -> None:
        with pytest.raises(GatewayError) as excinfo:
            await build(rate_limited(), unavailable("backup")).complete(REQUEST)
        error = excinfo.value
        assert error.status_code == 503
        assert error.code == "no_provider_available"

    async def test_only_one_failover_hop(self) -> None:
        primary, backup = rate_limited(), unavailable("backup")
        with pytest.raises(GatewayError):
            await build(primary, backup).complete(REQUEST)
        assert primary.complete_calls == 1
        assert backup.complete_calls == 1


class TestErrorSanitisation:
    async def test_the_public_message_leaks_nothing(self) -> None:
        primary = ScriptedProvider(
            name="primary",
            raises=RuntimeError(
                "connection to https://api.openai.com/v1 failed: "
                "key sk-proj-abc123 at /opt/gateway/providers.py line 88"
            ),
        )
        with pytest.raises(Exception) as excinfo:
            await build(primary, healthy()).complete(REQUEST)

        error = excinfo.value
        message = getattr(error, "message", str(error))
        for secret in ["sk-proj-abc123", "api.openai.com", "/opt/gateway", "line 88"]:
            assert secret not in message
        # Sanitised outward, intact inward. An operator given the error id must
        # still be able to find the real cause.
        assert "sk-proj-abc123" in error.internal_detail
        assert error.error_id

    async def test_the_cause_is_kept_internally(self) -> None:
        with pytest.raises(GatewayError) as excinfo:
            await build(rate_limited(), unavailable("backup")).complete(REQUEST)
        error = excinfo.value
        assert error.internal_detail
        assert error.error_id
        assert error.internal_detail != error.message


class TestStreamingRoutes:
    async def test_stream_uses_the_primary_when_healthy(self) -> None:
        primary = ScriptedProvider(name="primary", chunks=["a", "b", "c"])
        provider, iterator, failed_over = await build(primary, healthy()).stream(REQUEST)
        assert failed_over is False
        assert "".join([piece async for piece in iterator]) == "abc"

    async def test_stream_fails_over_on_429(self) -> None:
        backup = ScriptedProvider(name="backup", chunks=["x", "y"])
        provider, iterator, failed_over = await build(rate_limited(), backup).stream(REQUEST)
        assert failed_over is True
        assert "".join([piece async for piece in iterator]) == "xy"

    async def test_stream_fails_over_when_the_first_chunk_never_arrives(self) -> None:
        backup = ScriptedProvider(name="backup", chunks=["ok"])
        provider, iterator, failed_over = await build(hangs(seconds=30.0), backup).stream(
            REQUEST
        )
        assert failed_over is True
        assert "".join([piece async for piece in iterator]) == "ok"

    async def test_no_chunk_is_lost_to_the_deadline_check(self) -> None:
        """The first chunk is pulled to test the deadline, then replayed.

        If it were consumed and not replayed, every response would silently lose
        its opening words.
        """
        primary = ScriptedProvider(name="primary", chunks=["first ", "second ", "third"])
        _, iterator, _ = await build(primary, healthy()).stream(REQUEST)
        assert "".join([piece async for piece in iterator]) == "first second third"

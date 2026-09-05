"""Tests for the provider circuit breaker.

The behaviour being protected: during an outage the gateway must stop paying the
full timeout on every request, and when the provider recovers it must not be hit
by the entire backlog at once.
"""

import pytest

from src.llm_gateway.circuit_breaker import CircuitBreaker, State

THRESHOLD = 3
COOLDOWN = 30.0


@pytest.fixture()
def breaker() -> CircuitBreaker:
    return CircuitBreaker("test", failure_threshold=THRESHOLD, cooldown_seconds=COOLDOWN)


class TestClosedState:
    def test_starts_closed_and_allows_traffic(self, breaker: CircuitBreaker) -> None:
        assert breaker.status().state is State.CLOSED
        assert breaker.allows_request() is True

    def test_failures_below_the_threshold_do_not_open_it(
        self, breaker: CircuitBreaker
    ) -> None:
        for _ in range(THRESHOLD - 1):
            breaker.record_failure()
        assert breaker.status().state is State.CLOSED
        assert breaker.allows_request() is True

    def test_a_success_clears_the_failure_count(self, breaker: CircuitBreaker) -> None:
        """Consecutive failures, not cumulative.

        An intermittent failure every few hours must not eventually open the
        circuit on a provider that is fundamentally healthy.
        """
        breaker.record_failure()
        breaker.record_failure()
        breaker.record_success()
        assert breaker.status().consecutive_failures == 0
        breaker.record_failure()
        assert breaker.status().state is State.CLOSED


class TestOpening:
    def test_opens_at_the_threshold(self, breaker: CircuitBreaker) -> None:
        for _ in range(THRESHOLD):
            breaker.record_failure()
        assert breaker.status().state is State.OPEN

    def test_an_open_circuit_skips_the_provider(self, breaker: CircuitBreaker) -> None:
        """The whole point.

        Without this, every request during an outage waits the full timeout
        before failing over.
        """
        now = 1000.0
        for _ in range(THRESHOLD):
            breaker.record_failure(now)
        assert breaker.allows_request(now) is False
        assert breaker.allows_request(now + 1) is False
        assert breaker.allows_request(now + COOLDOWN - 1) is False

    def test_it_reports_when_it_will_retry(self, breaker: CircuitBreaker) -> None:
        now = 1000.0
        for _ in range(THRESHOLD):
            breaker.record_failure(now)
        status = breaker.status(now + 10)
        assert 19 <= status.seconds_until_retry <= 21


class TestHalfOpen:
    def test_the_cooldown_expiring_allows_a_probe(self, breaker: CircuitBreaker) -> None:
        now = 1000.0
        for _ in range(THRESHOLD):
            breaker.record_failure(now)
        assert breaker.allows_request(now + COOLDOWN + 1) is True
        assert breaker.status().state is State.HALF_OPEN

    def test_only_one_request_probes(self, breaker: CircuitBreaker) -> None:
        """No thundering herd.

        When the cooldown expires, exactly one request tests the provider. The
        rest keep using the backup until that probe reports back, so a
        recovering provider is not hit by the whole backlog at once.
        """
        now = 1000.0
        for _ in range(THRESHOLD):
            breaker.record_failure(now)

        later = now + COOLDOWN + 1
        assert breaker.allows_request(later) is True
        assert breaker.allows_request(later) is False
        assert breaker.allows_request(later) is False

    def test_a_successful_probe_closes_the_circuit(
        self, breaker: CircuitBreaker
    ) -> None:
        now = 1000.0
        for _ in range(THRESHOLD):
            breaker.record_failure(now)
        breaker.allows_request(now + COOLDOWN + 1)
        breaker.record_success()

        assert breaker.status().state is State.CLOSED
        assert breaker.allows_request(now + COOLDOWN + 2) is True

    def test_a_failed_probe_restarts_the_cooldown(
        self, breaker: CircuitBreaker
    ) -> None:
        """A provider that is still down must not be probed again immediately."""
        now = 1000.0
        for _ in range(THRESHOLD):
            breaker.record_failure(now)

        probe_at = now + COOLDOWN + 1
        breaker.allows_request(probe_at)
        breaker.record_failure(probe_at)

        assert breaker.status().state is State.OPEN
        assert breaker.allows_request(probe_at + 1) is False
        assert breaker.allows_request(probe_at + COOLDOWN + 1) is True

    def test_a_failed_probe_does_not_inflate_the_failure_count(
        self, breaker: CircuitBreaker
    ) -> None:
        now = 1000.0
        for _ in range(THRESHOLD):
            breaker.record_failure(now)
        before = breaker.status().consecutive_failures

        probe_at = now + COOLDOWN + 1
        breaker.allows_request(probe_at)
        breaker.record_failure(probe_at)

        assert breaker.status().consecutive_failures == before


class TestRecoveryCycle:
    def test_a_full_outage_and_recovery(self, breaker: CircuitBreaker) -> None:
        now = 1000.0

        for _ in range(THRESHOLD):
            assert breaker.allows_request(now) is True
            breaker.record_failure(now)

        assert breaker.allows_request(now + 5) is False

        probe_at = now + COOLDOWN + 1
        assert breaker.allows_request(probe_at) is True
        breaker.record_success()

        assert breaker.allows_request(probe_at + 1) is True
        assert breaker.status().state is State.CLOSED

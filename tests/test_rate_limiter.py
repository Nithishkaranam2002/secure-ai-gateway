"""Tests for the token aware sliding window limiter."""

import time

import pytest

from src.core.database import get_connection
from src.llm_gateway.rate_limiter import (
    MINIMUM_CHARGE_TOKENS,
    WINDOW_SECONDS,
    check_and_reserve,
    estimate_tokens,
    release,
    reset_eviction_timer,
    settle,
    usage_in_window,
)

ACME = "tk_live_acme_9f2b"      # 50000 per minute
GLOBEX = "tk_live_globex_4d7a"  # 50000 per minute
TINY = "tk_live_tiny_1c3e"      # 2000 per minute


@pytest.fixture(autouse=True)
def clean_usage() -> None:
    """Each test starts with an empty usage table."""
    with get_connection() as connection:
        connection.execute("DELETE FROM token_usage")
    reset_eviction_timer()


class TestBasicAccounting:
    def test_a_request_within_budget_is_allowed(self) -> None:
        decision, reservation = check_and_reserve(TINY, 500)
        assert decision.allowed is True
        assert reservation is not None
        assert decision.remaining == 1500

    def test_settling_corrects_the_reservation_downward(self) -> None:
        now = time.time()
        _, reservation = check_and_reserve(TINY, 900, now)
        assert usage_in_window(TINY, now) == 900
        settle(reservation, 250)
        assert usage_in_window(TINY, now) == 250

    def test_settling_never_charges_below_the_floor(self) -> None:
        """Without a floor the limit is unenforceable.

        A caller can request a large max_tokens, receive a very short answer,
        and repeat indefinitely, because each settled charge is negligible.
        """
        now = time.time()
        _, reservation = check_and_reserve(TINY, 900, now)
        settle(reservation, 5)
        assert usage_in_window(TINY, now) == MINIMUM_CHARGE_TOKENS

    def test_releasing_removes_the_charge_entirely(self) -> None:
        now = time.time()
        _, reservation = check_and_reserve(TINY, 900, now)
        release(reservation)
        assert usage_in_window(TINY, now) == 0

    def test_reserving_holds_budget_before_the_call_completes(self) -> None:
        """Checking without reserving would let concurrent requests each see
        room that only one of them can actually have."""
        now = time.time()
        check_and_reserve(TINY, 1500, now)
        decision, _ = check_and_reserve(TINY, 1000, now)
        assert decision.allowed is False


class TestTheLimit:
    def test_exceeding_the_budget_is_refused(self) -> None:
        now = time.time()
        check_and_reserve(TINY, 2000, now)
        decision, reservation = check_and_reserve(TINY, 100, now)
        assert decision.allowed is False
        assert reservation is None

    def test_exactly_the_limit_is_allowed(self) -> None:
        decision, _ = check_and_reserve(TINY, 2000, time.time())
        assert decision.allowed is True

    def test_a_refusal_says_when_to_retry(self) -> None:
        now = time.time()
        check_and_reserve(TINY, 2000, now)
        decision, _ = check_and_reserve(TINY, 100, now)
        assert 0 < decision.retry_after_seconds <= WINDOW_SECONDS

    def test_an_unknown_key_is_refused(self) -> None:
        decision, reservation = check_and_reserve("not-a-tenant", 10)
        assert decision.allowed is False
        assert reservation is None


class TestTenantIsolation:
    def test_one_tenant_cannot_consume_another_budget(self) -> None:
        """In a shared gateway, isolation between customers is the product."""
        now = time.time()
        check_and_reserve(TINY, 2000, now)
        assert check_and_reserve(TINY, 100, now)[0].allowed is False
        assert check_and_reserve(ACME, 1000, now)[0].allowed is True
        assert usage_in_window(GLOBEX, now) == 0


class TestTheWindowSlides:
    def test_usage_leaves_the_window_as_time_passes(self) -> None:
        now = time.time()
        check_and_reserve(TINY, 2000, now)
        assert check_and_reserve(TINY, 100, now)[0].allowed is False
        assert check_and_reserve(TINY, 100, now + WINDOW_SECONDS + 1)[0].allowed is True

    def test_usage_still_counts_just_inside_the_window(self) -> None:
        now = time.time()
        check_and_reserve(TINY, 2000, now)
        assert check_and_reserve(TINY, 100, now + WINDOW_SECONDS - 1)[0].allowed is False

    def test_the_fixed_window_boundary_exploit_does_not_work(self) -> None:
        """The reason this is a sliding window and not a fixed one.

        With a fixed window resetting on the minute, a tenant spends its whole
        allowance at 59 seconds and its whole allowance again at 61, doubling
        its rate while never breaking the stated rule. Looking back a full
        window from the current instant refuses the second spend.
        """
        base = time.time()
        check_and_reserve(TINY, 2000, base + 59)
        decision, _ = check_and_reserve(TINY, 2000, base + 61)
        assert decision.allowed is False

    def test_partial_expiry_frees_partial_budget(self) -> None:
        base = time.time()
        check_and_reserve(TINY, 1000, base)
        check_and_reserve(TINY, 1000, base + 30)
        assert check_and_reserve(TINY, 500, base + 31)[0].allowed is False
        # The first spend has now aged out, the second has not.
        assert check_and_reserve(TINY, 500, base + 61)[0].allowed is True
        assert check_and_reserve(TINY, 600, base + 61)[0].allowed is False


class TestEviction:
    def test_expired_rows_are_deleted(self) -> None:
        """State that is never cleaned up grows until the disk fills."""
        old = time.time() - (WINDOW_SECONDS * 5)
        with get_connection() as connection:
            connection.executemany(
                "INSERT INTO token_usage (api_key, tokens, recorded_at) VALUES (?, ?, ?)",
                [(TINY, 10, old + index) for index in range(50)],
            )
            before = connection.execute(
                "SELECT COUNT(*) AS n FROM token_usage"
            ).fetchone()["n"]
        assert before == 50

        reset_eviction_timer()
        check_and_reserve(TINY, 100)

        with get_connection() as connection:
            after = connection.execute(
                "SELECT COUNT(*) AS n FROM token_usage"
            ).fetchone()["n"]
        assert after < before

    def test_rows_inside_the_window_survive_eviction(self) -> None:
        now = time.time()
        check_and_reserve(TINY, 500, now)
        reset_eviction_timer()
        check_and_reserve(TINY, 100, now)
        assert usage_in_window(TINY, now) >= 500


class TestEstimation:
    def test_the_estimate_covers_prompt_and_worst_case_reply(self) -> None:
        estimate = estimate_tokens(
            {"messages": [{"role": "user", "content": "x" * 400}], "max_tokens": 1000}
        )
        assert estimate >= 1100

    def test_the_estimate_is_never_below_the_floor(self) -> None:
        estimate = estimate_tokens(
            {"messages": [{"role": "user", "content": "hi"}], "max_tokens": 1}
        )
        assert estimate >= MINIMUM_CHARGE_TOKENS

    def test_a_missing_max_tokens_still_estimates_something(self) -> None:
        assert estimate_tokens({"messages": [{"role": "user", "content": "hi"}]}) > 0

    def test_a_malformed_request_does_not_crash_the_estimator(self) -> None:
        assert estimate_tokens({}) >= MINIMUM_CHARGE_TOKENS
        assert estimate_tokens({"messages": ["not a dict"]}) >= MINIMUM_CHARGE_TOKENS

"""Token aware sliding window rate limiter backed by on disk SQLite.

A fixed window resets on a clock boundary, which lets a tenant spend its whole
allowance at 10:00:59 and its whole allowance again at 10:01:01. A sliding
window never resets: it looks back exactly one window from the current instant,
so that second spend is correctly refused.

That requires knowing when each spend happened, which is why usage is stored as
individual timestamped rows rather than as a running total.

State lives in SQLite on disk so limits survive a restart. A counter held in
memory would hand every tenant a fresh allowance on each deploy.
"""

import time
import uuid
from dataclasses import dataclass

from src.core.config import settings
from src.core.database import get_connection
from src.core.logging_setup import get_logger

logger = get_logger(__name__)

WINDOW_SECONDS = 60.0

# Rows are deleted well past the window rather than exactly at it, so a row is
# never removed while a concurrent query might still need it.
EVICTION_GRACE_SECONDS = 60.0

# Eviction runs on a timer rather than on every request. Sweeping per request
# would mean a delete statement for every call forever.
EVICTION_INTERVAL_SECONDS = 10.0

# Minimum charge per request.
#
# Settling to the provider's true count is correct, but on its own it leaves the
# limit unenforceable: a caller can request max_tokens=900, receive a 20 token
# answer, and repeat indefinitely, because each settled charge is negligible. A
# floor means a burst of small requests still consumes budget, which is what the
# limit exists to control.
MINIMUM_CHARGE_TOKENS = 100

_last_eviction = 0.0


@dataclass(frozen=True)
class LimitDecision:
    allowed: bool
    tenant: str
    limit: int
    used: int
    requested: int
    retry_after_seconds: float = 0.0

    @property
    def remaining(self) -> int:
        return max(0, self.limit - self.used)


def _limit_for(api_key: str) -> int | None:
    """The tenant's limit, or None if the key is not a known tenant."""
    with get_connection() as connection:
        row = connection.execute(
            "SELECT token_limit_per_minute FROM tenants WHERE api_key = ?",
            (api_key,),
        ).fetchone()
    if row is None:
        return None
    return int(row["token_limit_per_minute"])


def _evict_if_due(connection, now: float) -> None:
    global _last_eviction
    if now - _last_eviction < EVICTION_INTERVAL_SECONDS:
        return
    cutoff = now - WINDOW_SECONDS - EVICTION_GRACE_SECONDS
    deleted = connection.execute(
        "DELETE FROM token_usage WHERE recorded_at < ?", (cutoff,)
    ).rowcount
    _last_eviction = now
    if deleted:
        logger.info("evicted %d expired usage rows", deleted)


def usage_in_window(api_key: str, now: float | None = None) -> int:
    now = now if now is not None else time.time()
    window_start = now - WINDOW_SECONDS
    with get_connection() as connection:
        row = connection.execute(
            "SELECT COALESCE(SUM(tokens), 0) AS total FROM token_usage "
            "WHERE api_key = ? AND recorded_at >= ?",
            (api_key, window_start),
        ).fetchone()
    return int(row["total"])


def oldest_in_window(api_key: str, now: float) -> float | None:
    with get_connection() as connection:
        row = connection.execute(
            "SELECT MIN(recorded_at) AS oldest FROM token_usage "
            "WHERE api_key = ? AND recorded_at >= ?",
            (api_key, now - WINDOW_SECONDS),
        ).fetchone()
    return row["oldest"]


def check_and_reserve(
    api_key: str, estimated_tokens: int, now: float | None = None
) -> tuple[LimitDecision, str | None]:
    """Decide, and if allowed, hold the estimate against the tenant's budget.

    Returns the decision and a reservation id. The reservation must later be
    settled with the true token count, or released if the request failed.

    Checking without reserving would let concurrent requests each see room that
    only one of them can have.
    """
    now = now if now is not None else time.time()

    limit = _limit_for(api_key)
    if limit is None:
        return (
            LimitDecision(
                allowed=False,
                tenant=api_key,
                limit=0,
                used=0,
                requested=estimated_tokens,
            ),
            None,
        )

    try:
        with get_connection() as connection:
            _evict_if_due(connection, now)

            row = connection.execute(
                "SELECT COALESCE(SUM(tokens), 0) AS total FROM token_usage "
                "WHERE api_key = ? AND recorded_at >= ?",
                (api_key, now - WINDOW_SECONDS),
            ).fetchone()
            used = int(row["total"])

            if used + estimated_tokens > limit:
                oldest = connection.execute(
                    "SELECT MIN(recorded_at) AS oldest FROM token_usage "
                    "WHERE api_key = ? AND recorded_at >= ?",
                    (api_key, now - WINDOW_SECONDS),
                ).fetchone()["oldest"]
                # When the oldest spend leaves the window, room reappears.
                retry_after = (
                    max(0.0, (oldest + WINDOW_SECONDS) - now) if oldest else 1.0
                )
                return (
                    LimitDecision(
                        allowed=False,
                        tenant=api_key,
                        limit=limit,
                        used=used,
                        requested=estimated_tokens,
                        retry_after_seconds=round(retry_after, 1),
                    ),
                    None,
                )

            reservation_id = uuid.uuid4().hex
            connection.execute(
                "INSERT INTO token_usage (id, api_key, tokens, recorded_at) "
                "VALUES (NULL, ?, ?, ?)",
                (f"{api_key}", estimated_tokens, now),
            )
            reservation_row_id = connection.execute(
                "SELECT last_insert_rowid() AS id"
            ).fetchone()["id"]

    except Exception:
        # A monitoring component must not take down the service it monitors. If
        # the store is unavailable the request is allowed through and the
        # failure is logged, rather than every tenant being blocked by a
        # bookkeeping fault. The cost is that a limit can be exceeded during an
        # outage, which is the lesser harm.
        logger.exception("rate limiter unavailable, allowing request")
        return (
            LimitDecision(
                allowed=True,
                tenant=api_key,
                limit=limit,
                used=0,
                requested=estimated_tokens,
            ),
            None,
        )

    return (
        LimitDecision(
            allowed=True,
            tenant=api_key,
            limit=limit,
            used=used + estimated_tokens,
            requested=estimated_tokens,
        ),
        str(reservation_row_id),
    )


def settle(reservation_id: str | None, actual_tokens: int) -> None:
    """Correct a reservation to the true cost once the provider reports it.

    Charged at no less than the floor, so many cheap requests still add up.
    """
    if reservation_id is None:
        return
    charge = max(actual_tokens, MINIMUM_CHARGE_TOKENS)
    try:
        with get_connection() as connection:
            connection.execute(
                "UPDATE token_usage SET tokens = ? WHERE id = ?",
                (charge, int(reservation_id)),
            )
    except Exception:
        logger.exception("could not settle reservation %s", reservation_id)


def release(reservation_id: str | None) -> None:
    """Drop a reservation for a request that never happened."""
    if reservation_id is None:
        return
    try:
        with get_connection() as connection:
            connection.execute(
                "DELETE FROM token_usage WHERE id = ?", (int(reservation_id),)
            )
    except Exception:
        logger.exception("could not release reservation %s", reservation_id)


def estimate_tokens(request: dict) -> int:
    """Rough cost before the call, since the answer's size is not yet known.

    Roughly four characters per token for the prompt, plus the caller's
    max_tokens as the worst case for the reply. Deliberately pessimistic: an
    estimate that is too low lets tenants past their limit, while one that is
    too high is corrected downward the moment the provider reports the truth.
    """
    messages = request.get("messages") or []
    characters = sum(
        len(str(message.get("content", ""))) for message in messages if isinstance(message, dict)
    )
    prompt_estimate = max(1, characters // 4)
    completion_estimate = int(request.get("max_tokens") or 512)
    return max(MINIMUM_CHARGE_TOKENS, prompt_estimate + completion_estimate)


def reset_eviction_timer() -> None:
    """Test hook so eviction can be forced rather than waited for."""
    global _last_eviction
    _last_eviction = 0.0

"""Circuit breaker for provider health.

Without one, every request during a provider outage pays the full timeout before
failing over. A hundred requests means a hundred wasted waits and a hundred
connections held open, so the gateway stays slow for the whole outage despite
knowing after the first failure that the provider is down.

Three states:

  closed     normal, traffic flows to the provider
  open       the provider is skipped entirely until the cooldown expires
  half open   one request is let through to test recovery

The half open state is what keeps this from being a thundering herd. When the
cooldown expires, exactly one request probes the provider; the rest keep using
the backup until that probe reports back.
"""

import threading
import time
from dataclasses import dataclass
from enum import Enum

from src.core.logging_setup import get_logger

logger = get_logger(__name__)

FAILURE_THRESHOLD = 3
COOLDOWN_SECONDS = 30.0


class State(str, Enum):
    CLOSED = "closed"
    OPEN = "open"
    HALF_OPEN = "half_open"


@dataclass
class BreakerStatus:
    name: str
    state: State
    consecutive_failures: int
    opened_at: float | None
    seconds_until_retry: float


class CircuitBreaker:
    def __init__(
        self,
        name: str,
        failure_threshold: int = FAILURE_THRESHOLD,
        cooldown_seconds: float = COOLDOWN_SECONDS,
    ) -> None:
        self.name = name
        self.failure_threshold = failure_threshold
        self.cooldown_seconds = cooldown_seconds
        self._state = State.CLOSED
        self._consecutive_failures = 0
        self._opened_at: float | None = None
        self._probe_in_flight = False
        self._lock = threading.Lock()

    def allows_request(self, now: float | None = None) -> bool:
        """Whether to attempt this provider at all."""
        now = now if now is not None else time.time()
        with self._lock:
            if self._state is State.CLOSED:
                return True

            if self._state is State.OPEN:
                if self._opened_at is None:
                    return True
                if now - self._opened_at < self.cooldown_seconds:
                    return False
                # Cooldown has expired. Move to half open and let exactly one
                # request probe the provider.
                self._state = State.HALF_OPEN
                self._probe_in_flight = True
                logger.info("circuit for %s is half open, probing", self.name)
                return True

            # Half open. Only the probe proceeds; everything else waits for it,
            # so a recovering provider is not hit by the full backlog at once.
            if self._probe_in_flight:
                return False
            self._probe_in_flight = True
            return True

    def record_success(self) -> None:
        with self._lock:
            was = self._state
            self._state = State.CLOSED
            self._consecutive_failures = 0
            self._opened_at = None
            self._probe_in_flight = False
            if was is not State.CLOSED:
                logger.info("circuit for %s closed after a successful probe", self.name)

    def record_failure(self, now: float | None = None) -> None:
        now = now if now is not None else time.time()
        with self._lock:
            self._probe_in_flight = False

            if self._state is State.HALF_OPEN:
                # The probe failed, so the provider is still down. Restart the
                # cooldown rather than counting toward the threshold again.
                self._state = State.OPEN
                self._opened_at = now
                logger.warning("circuit for %s reopened, probe failed", self.name)
                return

            self._consecutive_failures += 1
            if self._consecutive_failures >= self.failure_threshold:
                self._state = State.OPEN
                self._opened_at = now
                logger.warning(
                    "circuit for %s opened after %d consecutive failures",
                    self.name,
                    self._consecutive_failures,
                )

    def status(self, now: float | None = None) -> BreakerStatus:
        now = now if now is not None else time.time()
        with self._lock:
            remaining = 0.0
            if self._state is State.OPEN and self._opened_at is not None:
                remaining = max(0.0, self.cooldown_seconds - (now - self._opened_at))
            return BreakerStatus(
                name=self.name,
                state=self._state,
                consecutive_failures=self._consecutive_failures,
                opened_at=self._opened_at,
                seconds_until_retry=round(remaining, 1),
            )

    def reset(self) -> None:
        """Test hook."""
        with self._lock:
            self._state = State.CLOSED
            self._consecutive_failures = 0
            self._opened_at = None
            self._probe_in_flight = False

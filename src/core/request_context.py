"""One id per request, available anywhere without being passed through.

A request crosses the gateway, the policy, the bridge and the downstream server,
and each of those writes its own log lines and audit rows. Without a shared id
those records cannot be tied together, so tracing one request means guessing
from timestamps.

A ContextVar carries the id, which means it is per task rather than per thread
and does not leak between concurrent requests handled on the same event loop.
"""

import uuid
from contextvars import ContextVar

_correlation_id: ContextVar[str | None] = ContextVar("correlation_id", default=None)

HEADER_NAME = "X-Correlation-ID"


def new_id() -> str:
    return uuid.uuid4().hex[:16]


def set_correlation_id(value: str | None = None) -> str:
    """Start a new request scope, honouring an inbound id if one was supplied.

    Accepting the caller's id lets a trace span more than this service, which is
    what makes it useful once the gateway sits inside a larger system.
    """
    resolved = (value or "").strip() or new_id()
    _correlation_id.set(resolved)
    return resolved


def get_correlation_id() -> str | None:
    return _correlation_id.get()

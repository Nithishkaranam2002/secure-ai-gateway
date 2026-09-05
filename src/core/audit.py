import json
from datetime import datetime, timezone
from typing import Any

from src.core.database import get_connection
from src.core.errors import new_error_id
from src.core.logging_setup import get_logger
from src.core.request_context import get_correlation_id

logger = get_logger(__name__)


def record(
    component: str,
    action: str,
    decision: str,
    actor: str | None = None,
    detail: dict[str, Any] | None = None,
    event_id: str | None = None,
) -> str:
    """Write one decision to the audit log and return its event id.

    Never pass a redacted value or a secret in detail. Record what happened
    and how much, not the sensitive content itself.
    """
    event_id = event_id or new_error_id()
    occurred_at = datetime.now(timezone.utc).isoformat()

    # The correlation id rides in the detail column rather than in a new column,
    # so existing rows stay readable and no migration is needed.
    correlation_id = get_correlation_id()
    payload = dict(detail) if detail else {}
    if correlation_id:
        payload["correlation_id"] = correlation_id
    serialised = json.dumps(payload, default=str) if payload else None

    try:
        with get_connection() as connection:
            connection.execute(
                "INSERT INTO audit_log "
                "(event_id, occurred_at, component, action, decision, actor, detail) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (event_id, occurred_at, component, action, decision, actor, serialised),
            )
    except Exception as exc:
        # Auditing must never break the request path.
        logger.error("audit write failed event_id=%s error=%s", event_id, exc)

    logger.info(
        "audit component=%s action=%s decision=%s actor=%s event_id=%s corr=%s",
        component,
        action,
        decision,
        actor,
        event_id,
        correlation_id or "-",
    )
    return event_id

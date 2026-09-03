"""Business logic for the MCP tools.

Nothing in this file knows about JSON-RPC or stdio. Input arriving here has
already passed schema validation, so anything that fails at this level is a
well formed request that cannot be completed, not a malformed one.
"""

import uuid
from datetime import datetime, timezone
from typing import Any

from src.core.audit import record
from src.core.database import get_connection
from src.core.logging_setup import get_logger

logger = get_logger(__name__)

COMPONENT = "mcp_server"

# A ceiling on what an automated agent may refund without a human. An agent that
# has been talked into a large refund is stopped here even though its request is
# perfectly well formed.
MAX_AUTOMATED_REFUND = 10_000.00


class ToolExecutionError(Exception):
    """A well formed request that cannot be completed.

    Returned to the caller as a tool result with the error flag set, so the
    model can read the explanation and correct itself. This is deliberately not
    a JSON-RPC protocol error, which is reserved for malformed messages.
    """

    def __init__(self, message: str, code: str) -> None:
        super().__init__(message)
        self.message = message
        self.code = code


def get_customer_record(customer_id: str) -> dict[str, Any]:
    with get_connection() as connection:
        row = connection.execute(
            "SELECT customer_id, name, email, plan, status, created_at "
            "FROM customers WHERE customer_id = ?",
            (customer_id,),
        ).fetchone()

    if row is None:
        record(
            COMPONENT,
            "get_customer_record",
            "not_found",
            actor=customer_id,
        )
        raise ToolExecutionError(
            f"No customer exists with id {customer_id}.",
            "customer_not_found",
        )

    record(COMPONENT, "get_customer_record", "allowed", actor=customer_id)
    return dict(row)


def trigger_refund(customer_id: str, amount: float, reason: str) -> dict[str, Any]:
    # Read, decide, then write. Each step uses its own connection and none is
    # held open across another, which keeps SQLite out of lock contention with
    # the audit writer.
    with get_connection() as connection:
        customer = connection.execute(
            "SELECT customer_id, status FROM customers WHERE customer_id = ?",
            (customer_id,),
        ).fetchone()

    if customer is None:
        record(COMPONENT, "trigger_refund", "not_found", actor=customer_id)
        raise ToolExecutionError(
            f"No customer exists with id {customer_id}.",
            "customer_not_found",
        )

    if customer["status"] != "active":
        record(
            COMPONENT,
            "trigger_refund",
            "blocked",
            actor=customer_id,
            detail={"status": customer["status"]},
        )
        raise ToolExecutionError(
            f"Customer {customer_id} has status {customer['status']} "
            "and cannot receive a refund.",
            "customer_not_active",
        )

    if amount > MAX_AUTOMATED_REFUND:
        record(
            COMPONENT,
            "trigger_refund",
            "blocked",
            actor=customer_id,
            detail={"amount": amount, "ceiling": MAX_AUTOMATED_REFUND},
        )
        raise ToolExecutionError(
            f"Refunds above {MAX_AUTOMATED_REFUND:.2f} require human approval.",
            "refund_ceiling_exceeded",
        )

    refund_id = f"REF-{uuid.uuid4().hex[:10].upper()}"
    created_at = datetime.now(timezone.utc).isoformat()

    with get_connection() as connection:
        connection.execute(
            "INSERT INTO refunds (refund_id, customer_id, amount, reason, created_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (refund_id, customer_id, amount, reason, created_at),
        )

    record(
        COMPONENT,
        "trigger_refund",
        "allowed",
        actor=customer_id,
        detail={"refund_id": refund_id, "amount": amount},
    )

    return {
        "refund_id": refund_id,
        "customer_id": customer_id,
        "amount": amount,
        "reason": reason,
        "status": "issued",
        "created_at": created_at,
    }

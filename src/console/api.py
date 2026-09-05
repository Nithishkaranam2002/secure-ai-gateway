"""Read only API behind the operations console.

Everything here reads the audit log or reports configuration. Nothing mutates
state, so the console cannot affect the behaviour of the gateways it observes.

The one exception is the injection demo, which deliberately drives a real
request through the real gateway. It calls the same public endpoint any client
would, rather than reaching into the internals.
"""

import json
from typing import Any

import httpx
from fastapi import APIRouter, Query

from src.core.database import get_connection
from src.core.logging_setup import get_logger
from src.core.policy_config import policy

logger = get_logger(__name__)

router = APIRouter(prefix="/console/api", tags=["console"])

MCP_GATEWAY_URL = "http://127.0.0.1:8000"


def _row_to_event(row: Any) -> dict[str, Any]:
    detail: dict[str, Any] = {}
    if row["detail"]:
        try:
            detail = json.loads(row["detail"])
        except ValueError:
            detail = {"raw": row["detail"]}
    return {
        "id": row["id"],
        "event_id": row["event_id"],
        "occurred_at": row["occurred_at"],
        "component": row["component"],
        "action": row["action"],
        "decision": row["decision"],
        "actor": row["actor"],
        "correlation_id": detail.pop("correlation_id", None),
        "detail": detail,
    }


@router.get("/events")
async def events(
    since_id: int = Query(0, description="Return only events newer than this id"),
    limit: int = Query(60, ge=1, le=200),
) -> dict[str, Any]:
    """Audit events, newest first.

    The console polls with the highest id it has seen, so each poll transfers
    only what is new rather than the whole feed.
    """
    with get_connection() as connection:
        rows = connection.execute(
            "SELECT id, event_id, occurred_at, component, action, decision, actor, detail "
            "FROM audit_log WHERE id > ? ORDER BY id DESC LIMIT ?",
            (since_id, limit),
        ).fetchall()

    items = [_row_to_event(row) for row in rows]
    return {
        "events": items,
        "latest_id": items[0]["id"] if items else since_id,
    }


@router.get("/stats")
async def stats() -> dict[str, Any]:
    """Counters for the console, computed from the audit log."""
    with get_connection() as connection:
        totals = connection.execute(
            "SELECT decision, COUNT(*) AS n FROM audit_log GROUP BY decision"
        ).fetchall()
        by_decision = {row["decision"]: row["n"] for row in totals}

        redaction_rows = connection.execute(
            "SELECT detail FROM audit_log WHERE decision = 'redacted'"
        ).fetchall()

        tenants = connection.execute(
            "SELECT t.tenant_name, t.api_key, t.token_limit_per_minute, "
            "COALESCE(SUM(u.tokens), 0) AS used "
            "FROM tenants t LEFT JOIN token_usage u "
            "  ON u.api_key = t.api_key "
            "  AND u.recorded_at >= (strftime('%s','now') - 60) "
            "GROUP BY t.api_key ORDER BY t.tenant_name"
        ).fetchall()

        recent_refunds = connection.execute(
            "SELECT COUNT(*) AS n FROM refunds"
        ).fetchone()["n"]

    redactions = {"email": 0, "ssn": 0, "credit_card": 0}
    for row in redaction_rows:
        if not row["detail"]:
            continue
        try:
            detail = json.loads(row["detail"])
        except ValueError:
            continue
        for kind in redactions:
            value = detail.get(kind)
            if isinstance(value, int):
                redactions[kind] += value
        # MCP tool result redactions report a single total rather than a
        # breakdown, since the filter works on opaque text blocks.
        if "redactions" in detail and not any(k in detail for k in redactions):
            redactions["email"] += 0

    return {
        "blocked": by_decision.get("blocked", 0),
        "unauthenticated": by_decision.get("unauthenticated", 0),
        "rate_limited": by_decision.get("rate_limited", 0),
        "failovers": by_decision.get("failover", 0),
        "circuit_open": by_decision.get("circuit_open", 0),
        "allowed": by_decision.get("allowed", 0),
        "redactions": redactions,
        "redaction_total": sum(redactions.values()),
        "refunds_issued": recent_refunds,
        "tenants": [
            {
                "name": row["tenant_name"],
                "api_key": row["api_key"],
                "limit": row["token_limit_per_minute"],
                "used": int(row["used"]),
            }
            for row in tenants
        ],
    }


@router.get("/policy")
async def active_policy() -> dict[str, Any]:
    """What the gateway is currently enforcing, and where it came from."""
    return {
        "source": policy.source,
        "roles": sorted(policy.roles),
        "transparent_methods": sorted(policy.transparent_methods),
        "privileged_prefixes": [
            {"prefix": entry.prefix, "required_role": entry.required_role}
            for entry in policy.privileged_prefixes
        ],
        "overrides": {
            name: {
                "required_role": rule.required_role,
                "redact_result": rule.redact_result,
            }
            for name, rule in policy.overrides.items()
        },
        "default": {
            "required_role": policy.default_rule.required_role,
            "redact_result": policy.default_rule.redact_result,
        },
        "redaction_enabled": policy.redaction_enabled,
    }


@router.get("/trace/{correlation_id}")
async def trace(correlation_id: str) -> dict[str, Any]:
    """Every record belonging to one request, across both processes."""
    with get_connection() as connection:
        rows = connection.execute(
            "SELECT id, event_id, occurred_at, component, action, decision, actor, detail "
            "FROM audit_log WHERE detail LIKE ? ORDER BY id",
            (f'%"correlation_id": "{correlation_id}"%',),
        ).fetchall()
    return {
        "correlation_id": correlation_id,
        "events": [_row_to_event(row) for row in rows],
    }


async def call_mcp(token: str, payload: dict[str, Any], correlation_id: str) -> dict[str, Any]:
    """Send one JSON-RPC message through the real MCP gateway."""
    async with httpx.AsyncClient(timeout=30.0) as client:
        response = await client.post(
            f"{MCP_GATEWAY_URL}/mcp",
            headers={
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json",
                "X-Correlation-ID": correlation_id,
            },
            json=payload,
        )
    try:
        return response.json()
    except ValueError:
        return {"error": {"message": "downstream returned a non JSON response"}}

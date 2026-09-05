"""MCP security gateway.

An HTTP JSON-RPC reverse proxy in front of the stdio MCP server. Every request
is authenticated, evaluated against the policy, and only then forwarded. A
request the policy refuses never reaches the downstream server at all, which is
the point: by the time a privileged tool has executed, blocking it is too late.
"""

import json
from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, Response

from src.core.audit import record
from src.core.database import initialise_database
from src.core.errors import (
    INTERNAL_ERROR,
    INVALID_REQUEST,
    PARSE_ERROR,
    UNAUTHENTICATED,
    jsonrpc_error,
)
from src.core.logging_setup import get_logger
from src.core.request_context import (
    HEADER_NAME,
    get_correlation_id,
    set_correlation_id,
)
from src.mcp_gateway.auth import AuthError, Principal, authenticate
from src.mcp_gateway.policy import evaluate
from src.mcp_gateway.response_filter import redact_tool_result
from src.mcp_gateway.stdio_bridge import BridgeError, bridge

logger = get_logger(__name__)

COMPONENT = "mcp_gateway"

# The bridge performs the handshake once on behalf of every client, so a client
# sending its own initialize is answered from the stored result rather than
# re-initialising the shared downstream session.
LOCALLY_ANSWERED = frozenset({"initialize"})
SWALLOWED_NOTIFICATIONS = frozenset({"notifications/initialized"})


@asynccontextmanager
async def lifespan(app: FastAPI):
    initialise_database()
    try:
        await bridge.ensure_started()
    except Exception:
        # A downstream that is not up yet must not stop the gateway from
        # starting. The next request retries it and reports the failure.
        logger.exception("downstream MCP server did not start at boot")
    yield
    await bridge.stop()


app = FastAPI(
    title="MCP Security Gateway",
    description="Authenticated, policy filtered proxy in front of an MCP server.",
    version="1.0.0",
    lifespan=lifespan,
)


def _rpc(payload: dict[str, Any], status_code: int = 200) -> JSONResponse:
    response = JSONResponse(status_code=status_code, content=payload)
    correlation_id = get_correlation_id()
    if correlation_id:
        response.headers[HEADER_NAME] = correlation_id
    return response


@app.get("/")
async def root() -> dict[str, Any]:
    """A landing response, so someone opening the base URL is not met with a
    bare not found."""
    return {
        "service": "MCP Security Gateway",
        "version": "1.0.0",
        "endpoints": {
            "POST /mcp": "Authenticated MCP JSON-RPC proxy",
            "GET /health": "Liveness and downstream status",
            "GET /docs": "Interactive API documentation",
        },
    }


@app.get("/health")
async def health() -> dict[str, Any]:
    return {
        "status": "ok",
        "downstream_running": bridge.is_running(),
        "downstream_initialised": bridge.initialize_result is not None,
    }


@app.post("/mcp")
async def handle_mcp(request: Request) -> Response:
    # An inbound id is honoured so a trace can span more than this service.
    correlation_id = set_correlation_id(request.headers.get(HEADER_NAME))
    raw = await request.body()

    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        logger.warning("request body was not valid JSON")
        return _rpc(jsonrpc_error(None, PARSE_ERROR, "Parse error"))

    if isinstance(payload, list):
        # The current MCP revision removed JSON-RPC batching. Refusing is safer
        # than partially authorising a batch.
        return _rpc(
            jsonrpc_error(None, INVALID_REQUEST, "Batch requests are not supported")
        )

    if not isinstance(payload, dict):
        return _rpc(jsonrpc_error(None, INVALID_REQUEST, "Invalid Request"))

    request_id = payload.get("id")
    is_notification = "id" not in payload
    method = payload.get("method")
    params = payload.get("params")

    if not isinstance(method, str) or not method:
        return _rpc(jsonrpc_error(request_id, INVALID_REQUEST, "Invalid Request"))

    if params is not None and not isinstance(params, dict):
        return _rpc(jsonrpc_error(request_id, INVALID_REQUEST, "Invalid Request"))

    # ------------------------------------------------------------ authenticate
    try:
        principal: Principal = authenticate(request.headers.get("authorization"))
    except AuthError as exc:
        record(
            COMPONENT,
            method,
            "unauthenticated",
            actor=None,
            detail={"reason": exc.reason},
        )
        if is_notification:
            return Response(status_code=401)
        return _rpc(
            jsonrpc_error(request_id, UNAUTHENTICATED, exc.public_message),
            status_code=401,
        )

    actor = f"{principal.subject}:{principal.role}"

    # ---------------------------------------------------------------- authorise
    decision = evaluate(method, params, principal)

    if not decision.allowed:
        record(
            COMPONENT,
            method,
            "blocked",
            actor=actor,
            detail={"tool": decision.tool_name, "reason": decision.reason},
        )
        logger.warning("blocked %s for %s: %s", method, actor, decision.reason)
        if is_notification:
            return Response(status_code=204)
        # Deliberately 200. The brief asks for a JSON-RPC error, and MCP clients
        # read the body. The refusal is in the payload, not the status line.
        return _rpc(
            jsonrpc_error(
                request_id,
                decision.error_code or INVALID_REQUEST,
                decision.error_message or "Request refused",
            )
        )

    record(
        COMPONENT,
        method,
        "allowed",
        actor=actor,
        detail={"tool": decision.tool_name} if decision.tool_name else None,
    )

    # ------------------------------------------------------------------ forward
    if is_notification:
        if method in SWALLOWED_NOTIFICATIONS:
            # The shared session is already initialised. Forwarding a second
            # initialized notification would confuse the downstream server.
            return Response(status_code=202)
        try:
            await bridge.forward_notification(method, params)
        except BridgeError as exc:
            logger.error("could not forward notification %s: %s", method, exc)
        return Response(status_code=202)

    if method in LOCALLY_ANSWERED:
        try:
            await bridge.ensure_started()
        except Exception as exc:
            return _handle_bridge_failure(request_id, method, actor, exc)
        return _rpc(
            {"jsonrpc": "2.0", "id": request_id, "result": bridge.initialize_result}
        )

    try:
        response = await bridge.forward_request(request_id, method, params)
    except BridgeError as exc:
        return _handle_bridge_failure(request_id, method, actor, exc)
    except Exception as exc:  # pragma: no cover
        return _handle_bridge_failure(request_id, method, actor, exc)

    # Tool results leave the trust boundary here, so they pass the same
    # guardrail a model response does. A customer record read through a tool is
    # exactly as sensitive as one repeated by a model.
    if method == "tools/call":
        # The gateway's allow only means the caller was permitted to try. The
        # downstream server can still refuse on schema or business grounds, and
        # an audit trail that records the permission without the outcome reads
        # as though every permitted call succeeded.
        downstream_error = response.get("error")
        if isinstance(downstream_error, dict):
            record(
                COMPONENT,
                "tools/call",
                "rejected_downstream",
                actor=actor,
                detail={
                    "tool": decision.tool_name,
                    "code": downstream_error.get("code"),
                },
            )

        response, counts = redact_tool_result(response)
        if counts.total:
            # Recorded by type, matching what the streaming path reports, so the
            # audit trail reads the same whichever route a value left by.
            record(
                COMPONENT,
                "tools/call",
                "redacted",
                actor=actor,
                detail={"tool": decision.tool_name, **counts.as_dict()},
            )

    return _rpc(response)


def _handle_bridge_failure(
    request_id: Any, method: str, actor: str | None, exc: Exception
) -> JSONResponse:
    """One flat message outward, the real cause in the log and audit trail."""
    event_id = record(
        COMPONENT,
        method,
        "upstream_failure",
        actor=actor,
        detail={"error": f"{type(exc).__name__}: {exc}"},
    )
    logger.error("downstream failure on %s: %s", method, exc)
    return _rpc(
        jsonrpc_error(
            request_id,
            INTERNAL_ERROR,
            "The downstream server could not be reached.",
            data={"error_id": event_id},
        ),
        status_code=502,
    )

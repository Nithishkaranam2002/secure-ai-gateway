"""LLM gateway.

One request path serves both halves of the work. A request is authenticated,
charged against its tenant's budget, routed to a provider that is actually
answering, and streamed back with sensitive values removed on the way out.

Errors leaving this service are short and carry an error id. The cause is in the
log and the audit table under that id, never in the response.
"""

import uuid
from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, StreamingResponse

from src.core.audit import record
from src.core.database import initialise_database
from src.core.errors import GatewayError, sanitise
from src.core.logging_setup import get_logger
from src.llm_gateway import rate_limiter
from src.llm_gateway.router import ModelRouter
from src.llm_gateway.stream_handler import redacted_stream
from src.mcp_gateway.auth import AuthError, Principal, authenticate

logger = get_logger(__name__)

COMPONENT = "llm_gateway"

router = ModelRouter()


@asynccontextmanager
async def lifespan(app: FastAPI):
    initialise_database()
    yield


app = FastAPI(
    title="LLM Gateway",
    description=(
        "Streaming LLM proxy with PII redaction, per tenant token rate "
        "limiting and automatic provider failover."
    ),
    version="1.0.0",
    lifespan=lifespan,
)


def _error_response(error: GatewayError) -> JSONResponse:
    return JSONResponse(status_code=error.status_code, content=error.public_payload())


@app.get("/")
async def root() -> dict[str, Any]:
    return {
        "service": "LLM Gateway",
        "version": "1.0.0",
        "endpoints": {
            "POST /v1/chat/completions": "Chat completions, streaming or not",
            "GET /health": "Liveness",
            "GET /v1/usage": "Token usage for the calling tenant",
            "GET /docs": "Interactive API documentation",
        },
    }


@app.get("/health")
async def health() -> dict[str, Any]:
    return {
        "status": "ok",
        "primary": router.primary.config.model,
        "backup": router.backup.config.model,
        "timeout_ms": int(router.timeout_seconds * 1000),
    }


def _principal_or_error(request: Request) -> Principal:
    try:
        return authenticate(request.headers.get("authorization"))
    except AuthError as exc:
        record(COMPONENT, "authenticate", "unauthenticated", detail={"reason": exc.reason})
        raise GatewayError(
            status_code=401,
            code="unauthenticated",
            message=exc.public_message,
            internal_detail=exc.reason,
        ) from exc


def _tenant_for(principal: Principal) -> str:
    if not principal.tenant:
        raise GatewayError(
            status_code=403,
            code="no_tenant",
            message="This credential is not associated with a tenant.",
            internal_detail=f"subject {principal.subject} has no tenant claim",
        )
    return principal.tenant


@app.get("/v1/usage")
async def usage(request: Request) -> Any:
    try:
        principal = _principal_or_error(request)
        tenant = _tenant_for(principal)
    except GatewayError as exc:
        return _error_response(exc)

    return {
        "tenant": tenant,
        "tokens_used_last_60s": rate_limiter.usage_in_window(tenant),
        "window_seconds": int(rate_limiter.WINDOW_SECONDS),
    }


@app.post("/v1/chat/completions")
async def chat_completions(request: Request) -> Any:
    response_id = f"chatcmpl-{uuid.uuid4().hex[:24]}"

    try:
        principal = _principal_or_error(request)
        tenant = _tenant_for(principal)
    except GatewayError as exc:
        return _error_response(exc)

    actor = f"{principal.subject}:{tenant}"

    try:
        body = await request.json()
    except Exception:
        return _error_response(
            GatewayError(
                status_code=400,
                code="invalid_request",
                message="Request body is not valid JSON.",
            )
        )

    if not isinstance(body, dict) or not isinstance(body.get("messages"), list):
        return _error_response(
            GatewayError(
                status_code=400,
                code="invalid_request",
                message="A messages array is required.",
            )
        )

    wants_stream = bool(body.get("stream", False))

    # ------------------------------------------------------------ rate limiting
    estimate = rate_limiter.estimate_tokens(body)
    decision, reservation = rate_limiter.check_and_reserve(tenant, estimate)

    if not decision.allowed:
        record(
            COMPONENT,
            "chat_completions",
            "rate_limited",
            actor=actor,
            detail={
                "limit": decision.limit,
                "used": decision.used,
                "requested": decision.requested,
            },
        )
        response = _error_response(
            GatewayError(
                status_code=429,
                code="rate_limit_exceeded",
                message=(
                    "Token budget exhausted for this tenant. "
                    f"Retry in {decision.retry_after_seconds:.0f} seconds."
                ),
                internal_detail=f"{decision.used}/{decision.limit} in window",
            )
        )
        response.headers["Retry-After"] = str(max(1, int(decision.retry_after_seconds)))
        return response

    # ------------------------------------------------------------------ routing
    if wants_stream:
        try:
            provider, source, failed_over = await router.stream(body, actor=actor)
        except GatewayError as exc:
            rate_limiter.release(reservation)
            logger.error("stream routing failed error_id=%s: %s", exc.error_id, exc.internal_detail)
            return _error_response(exc)
        except Exception as exc:
            rate_limiter.release(reservation)
            error = sanitise(exc)
            logger.error("stream routing failed error_id=%s: %s", error.error_id, error.internal_detail)
            return _error_response(error)

        # The streamed reply rarely carries a usage figure, so the reservation
        # stands as the charge. Noted in docs/decisions.md.
        record(
            COMPONENT,
            "chat_completions",
            "streaming",
            actor=actor,
            detail={
                "provider": provider.config.name,
                "failed_over": failed_over,
                "estimated_tokens": estimate,
            },
        )

        class _SourceProvider:
            """Adapts an already opened stream to the shape the handler expects."""

            def __init__(self, inner_provider, iterator) -> None:
                self.config = inner_provider.config
                self._iterator = iterator

            def stream(self, _request: dict[str, Any]):
                return self._iterator

        return StreamingResponse(
            redacted_stream(
                _SourceProvider(provider, source), body, actor, response_id
            ),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "X-Accel-Buffering": "no",
                "X-Gateway-Provider": provider.config.name,
                "X-Gateway-Failed-Over": str(failed_over).lower(),
            },
        )

    try:
        result = await router.complete(body, actor=actor)
    except GatewayError as exc:
        rate_limiter.release(reservation)
        logger.error("routing failed error_id=%s: %s", exc.error_id, exc.internal_detail)
        return _error_response(exc)
    except Exception as exc:
        rate_limiter.release(reservation)
        error = sanitise(exc)
        logger.error("routing failed error_id=%s: %s", error.error_id, error.internal_detail)
        return _error_response(error)

    # The provider reported the true cost, so the estimate is corrected to it.
    if result.total_tokens:
        rate_limiter.settle(reservation, result.total_tokens)

    from src.core.redaction import redact_text

    payload = dict(result.body)
    redaction_total = 0
    choices = []
    for choice in payload.get("choices", []):
        message = dict(choice.get("message") or {})
        content = message.get("content")
        if isinstance(content, str):
            cleaned, counts = redact_text(content)
            message["content"] = cleaned
            redaction_total += counts.total
        choices.append({**choice, "message": message})
    payload["choices"] = choices

    record(
        COMPONENT,
        "chat_completions",
        "redacted" if redaction_total else "clean",
        actor=actor,
        detail={
            "provider": result.provider_name,
            "model": result.model,
            "failed_over": result.failed_over,
            "tokens": result.total_tokens,
            "redactions": redaction_total,
        },
    )

    return JSONResponse(
        content=payload,
        headers={
            "X-Gateway-Provider": result.provider_name,
            "X-Gateway-Failed-Over": str(result.failed_over).lower(),
        },
    )

"""Joins the provider stream to the redactor and emits SSE.

Each chunk arrives, passes through the redactor, and is written out. Nothing is
kept. There is no list being appended to and no string being built up, which is
the memory requirement made visible: this file could stream a response of any
length in constant memory.

The output is the same SSE shape the provider sent, so a client cannot tell
there is a gateway in the middle.
"""

import json
import time
from collections.abc import AsyncIterator
from typing import Any

from src.core.audit import record
from src.core.logging_setup import get_logger
from src.llm_gateway.providers import Provider
from src.llm_gateway.redaction import StreamRedactor

logger = get_logger(__name__)

COMPONENT = "llm_gateway"
SSE_DONE = "data: [DONE]\n\n"


def sse_chunk(text: str, model: str, created: int, response_id: str) -> str:
    """Wrap a piece of text in the delta shape an OpenAI client expects."""
    payload = {
        "id": response_id,
        "object": "chat.completion.chunk",
        "created": created,
        "model": model,
        "choices": [{"index": 0, "delta": {"content": text}, "finish_reason": None}],
    }
    return f"data: {json.dumps(payload)}\n\n"


def sse_final(model: str, created: int, response_id: str) -> str:
    payload = {
        "id": response_id,
        "object": "chat.completion.chunk",
        "created": created,
        "model": model,
        "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
    }
    return f"data: {json.dumps(payload)}\n\n"


def sse_error(message: str, error_id: str) -> str:
    """An error raised after streaming began.

    Once the first chunk has gone out the status line is already 200 and cannot
    be taken back, so the only honest way to report a failure is inside the
    stream itself.
    """
    payload = {
        "error": {
            "code": "upstream_error",
            "message": message,
            "error_id": error_id,
        }
    }
    return f"data: {json.dumps(payload)}\n\n"


async def redacted_stream(
    provider: Provider,
    request: dict[str, Any],
    actor: str | None,
    response_id: str,
) -> AsyncIterator[str]:
    """Yield SSE lines carrying the provider's answer with sensitive values removed."""
    redactor = StreamRedactor()
    created = int(time.time())
    model = provider.config.model

    started_at = time.perf_counter()
    # Two separate measurements, because they answer different questions.
    # upstream_ttft is how long the provider took to say anything, which this
    # gateway does not control. gateway_overhead_ms is the delay this code adds
    # on top of it, which is the number the design is actually accountable for.
    upstream_first_chunk_at: float | None = None
    time_to_first_token: float | None = None
    emitted_chunks = 0

    try:
        async for piece in provider.stream(request):
            if upstream_first_chunk_at is None:
                upstream_first_chunk_at = time.perf_counter()
            safe = redactor.feed(piece)
            if not safe:
                continue
            if time_to_first_token is None:
                time_to_first_token = time.perf_counter() - started_at
            emitted_chunks += 1
            yield sse_chunk(safe, model, created, response_id)

        tail = redactor.flush()
        if tail:
            if time_to_first_token is None:
                time_to_first_token = time.perf_counter() - started_at
            emitted_chunks += 1
            yield sse_chunk(tail, model, created, response_id)

    except Exception as exc:
        # Whatever is still held back is dropped rather than released, because
        # unflushed text has not been checked and may contain the value the
        # redactor was waiting to complete.
        event_id = record(
            COMPONENT,
            "stream",
            "upstream_failure",
            actor=actor,
            detail={"error": f"{type(exc).__name__}: {exc}"},
        )
        logger.error("stream failed after %d chunks: %s", emitted_chunks, exc)
        yield sse_error("The response could not be completed.", event_id)
        yield SSE_DONE
        return

    yield sse_final(model, created, response_id)
    yield SSE_DONE

    counts = redactor.counts
    record(
        COMPONENT,
        "stream",
        "redacted" if counts.total else "clean",
        actor=actor,
        detail={
            "model": model,
            "chunks": emitted_chunks,
            "ttft_ms": round((time_to_first_token or 0) * 1000, 1),
            "upstream_ttft_ms": round(
                ((upstream_first_chunk_at or started_at) - started_at) * 1000, 1
            ),
            "gateway_overhead_ms": round(
                (
                    (time_to_first_token or 0)
                    - ((upstream_first_chunk_at or started_at) - started_at)
                )
                * 1000,
                1,
            ),
            # Counts only. The values themselves are never recorded, since
            # logging what was just redacted would defeat the point of it.
            **counts.as_dict(),
        },
    )
    logger.info(
        "stream complete chunks=%d ttft_ms=%.1f redactions=%d",
        emitted_chunks,
        (time_to_first_token or 0) * 1000,
        counts.total,
    )

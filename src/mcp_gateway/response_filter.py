"""Redaction of MCP tool results on the way back to the caller.

Task 3 removes sensitive values from model responses. Without this module the
MCP path would hand the same values over untouched, so an agent blocked from
reading an email address in a completion could simply call a tool and get it
directly. A guardrail that covers one route out and not the other is not a
guardrail.

The same engine serves both paths, which is why it lives in src/core rather
than under either gateway. Counts are reported by type rather than as a single
total, so the audit trail says the same thing whichever route a value left by.
"""

from typing import Any

from src.core.logging_setup import get_logger
from src.core.redaction import RedactionCounts, redact_text

logger = get_logger(__name__)


def redact_tool_result(response: dict[str, Any]) -> tuple[dict[str, Any], RedactionCounts]:
    """Clean the text blocks of a tools/call result.

    Returns the response and a per type count. The response is rebuilt rather
    than mutated, so a caller holding the original is unaffected. Anything that
    is not a text block is passed through untouched.
    """
    totals = RedactionCounts()

    result = response.get("result")
    if not isinstance(result, dict):
        return response, totals

    content = result.get("content")
    if not isinstance(content, list):
        return response, totals

    cleaned_blocks: list[Any] = []

    for block in content:
        if (
            isinstance(block, dict)
            and block.get("type") == "text"
            and isinstance(block.get("text"), str)
        ):
            cleaned, counts = redact_text(block["text"])
            totals.email += counts.email
            totals.ssn += counts.ssn
            totals.credit_card += counts.credit_card
            cleaned_blocks.append({**block, "text": cleaned})
        else:
            cleaned_blocks.append(block)

    if totals.total == 0:
        return response, totals

    return {**response, "result": {**result, "content": cleaned_blocks}}, totals

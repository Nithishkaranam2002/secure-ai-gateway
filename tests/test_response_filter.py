"""Tests for redaction of MCP tool results.

The gap these close: Task 3 scrubs model responses, so without this an agent
denied an email address in a completion could obtain the same address by calling
a tool instead.
"""

import json

from src.core.redaction import REDACTION_PLACEHOLDER
from src.mcp_gateway.response_filter import redact_tool_result


def tool_response(text: str, request_id: int = 1) -> dict:
    return {
        "jsonrpc": "2.0",
        "id": request_id,
        "result": {"content": [{"type": "text", "text": text}], "isError": False},
    }


def text_of(response: dict) -> str:
    return response["result"]["content"][0]["text"]


class TestRedaction:
    def test_an_email_in_a_customer_record_is_removed(self) -> None:
        record = json.dumps(
            {
                "customer_id": "CUST-10001",
                "name": "Amara Osei",
                "email": "amara.osei@example.com",
                "status": "active",
            }
        )
        cleaned, removed = redact_tool_result(tool_response(record))
        assert removed == 1
        assert "amara.osei@example.com" not in text_of(cleaned)
        assert REDACTION_PLACEHOLDER in text_of(cleaned)

    def test_the_rest_of_the_record_survives(self) -> None:
        """Redaction must not damage the fields the agent legitimately needs."""
        record = json.dumps(
            {
                "customer_id": "CUST-10001",
                "name": "Amara Osei",
                "email": "amara.osei@example.com",
                "status": "active",
            }
        )
        cleaned, _ = redact_tool_result(tool_response(record))
        text = text_of(cleaned)
        assert "CUST-10001" in text
        assert "Amara Osei" in text
        assert "active" in text

    def test_a_card_number_in_a_tool_result_is_removed(self) -> None:
        cleaned, removed = redact_tool_result(
            tool_response("Refund issued to card 4111 1111 1111 1111")
        )
        assert removed == 1
        assert "4111" not in text_of(cleaned)

    def test_multiple_blocks_are_each_cleaned(self) -> None:
        response = {
            "jsonrpc": "2.0",
            "id": 1,
            "result": {
                "content": [
                    {"type": "text", "text": "First: a@b.com"},
                    {"type": "text", "text": "Second: c@d.org"},
                ]
            },
        }
        cleaned, removed = redact_tool_result(response)
        assert removed == 2
        assert "@" not in json.dumps(cleaned["result"]["content"])


class TestPassThrough:
    def test_clean_results_are_returned_unchanged(self) -> None:
        response = tool_response('{"customer_id": "CUST-10001", "status": "active"}')
        cleaned, removed = redact_tool_result(response)
        assert removed == 0
        assert cleaned is response

    def test_an_error_response_is_left_alone(self) -> None:
        response = {
            "jsonrpc": "2.0",
            "id": 1,
            "error": {"code": -32001, "message": "Unauthorized Tool Call"},
        }
        cleaned, removed = redact_tool_result(response)
        assert cleaned == response
        assert removed == 0

    def test_a_non_text_block_is_untouched(self) -> None:
        response = {
            "jsonrpc": "2.0",
            "id": 1,
            "result": {
                "content": [
                    {"type": "image", "data": "base64data", "mimeType": "image/png"},
                    {"type": "text", "text": "Contact a@b.com"},
                ]
            },
        }
        cleaned, removed = redact_tool_result(response)
        assert removed == 1
        assert cleaned["result"]["content"][0] == response["result"]["content"][0]

    def test_a_malformed_result_does_not_crash(self) -> None:
        for response in [
            {"jsonrpc": "2.0", "id": 1},
            {"jsonrpc": "2.0", "id": 1, "result": "not a dict"},
            {"jsonrpc": "2.0", "id": 1, "result": {"content": "not a list"}},
            {"jsonrpc": "2.0", "id": 1, "result": {}},
        ]:
            cleaned, removed = redact_tool_result(response)
            assert removed == 0
            assert cleaned == response


class TestTheOriginalIsNotMutated:
    def test_the_caller_copy_is_unaffected(self) -> None:
        response = tool_response("Contact a@b.com")
        original = json.dumps(response)
        redact_tool_result(response)
        assert json.dumps(response) == original


class TestBothPathsAgree:
    def test_the_same_value_is_removed_whichever_route_it_leaves_by(self) -> None:
        """The point of the fix.

        A value that Task 3 removes from a model response must also be removed
        from a tool result, or the guardrail is only half a guardrail.
        """
        from src.core.redaction import redact_text

        value = "Reach Amara at amara.osei@example.com about card 4111 1111 1111 1111"
        via_llm, llm_counts = redact_text(value)
        via_mcp, mcp_counts = redact_tool_result(tool_response(value))

        assert text_of(via_mcp) == via_llm
        assert mcp_counts == llm_counts.total

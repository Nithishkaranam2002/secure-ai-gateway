"""Tests for the gateway authorisation policy."""

import pytest

from src.core.errors import INVALID_PARAMS, UNAUTHORIZED_TOOL_CALL
from src.mcp_gateway.auth import Principal
from src.mcp_gateway.policy import UNAUTHORIZED_MESSAGE, evaluate, is_admin_tool

VIEWER = Principal(subject="agent-1", role="viewer")
ADMIN = Principal(subject="ops-2", role="admin")


class TestAdminToolDetection:
    @pytest.mark.parametrize(
        "name",
        [
            "admin_reset_key",
            "admin_delete_tenant",
            "ADMIN_RESET_KEY",       # case must not be a bypass
            "Admin_Reset_Key",
            "  admin_reset_key  ",   # padding must not be a bypass
        ],
    )
    def test_recognises_privileged_names(self, name: str) -> None:
        assert is_admin_tool(name) is True

    @pytest.mark.parametrize(
        "name",
        [
            "get_customer_record",
            "trigger_refund",
            "administrator_report",  # starts with admin but not the prefix
            "reset_admin_key",       # the word appears, but not as the prefix
        ],
    )
    def test_leaves_ordinary_names_alone(self, name: str) -> None:
        assert is_admin_tool(name) is False


class TestTransparentMethods:
    @pytest.mark.parametrize("principal", [VIEWER, ADMIN])
    def test_tools_list_is_forwarded_for_every_role(self, principal: Principal) -> None:
        decision = evaluate("tools/list", None, principal)
        assert decision.allowed is True

    def test_initialize_is_forwarded(self) -> None:
        assert evaluate("initialize", {}, VIEWER).allowed is True


class TestToolCalls:
    def test_viewer_may_call_an_ordinary_tool(self) -> None:
        decision = evaluate(
            "tools/call",
            {"name": "get_customer_record", "arguments": {"customer_id": "CUST-10001"}},
            VIEWER,
        )
        assert decision.allowed is True
        assert decision.tool_name == "get_customer_record"

    def test_viewer_may_not_call_a_privileged_tool(self) -> None:
        decision = evaluate("tools/call", {"name": "admin_reset_key"}, VIEWER)
        assert decision.allowed is False
        assert decision.error_code == UNAUTHORIZED_TOOL_CALL
        assert decision.error_message == UNAUTHORIZED_MESSAGE

    def test_admin_may_call_a_privileged_tool(self) -> None:
        decision = evaluate("tools/call", {"name": "admin_reset_key"}, ADMIN)
        assert decision.allowed is True

    @pytest.mark.parametrize("name", ["ADMIN_RESET_KEY", " admin_reset_key"])
    def test_case_and_padding_do_not_bypass_the_check(self, name: str) -> None:
        decision = evaluate("tools/call", {"name": name}, VIEWER)
        assert decision.allowed is False
        assert decision.error_code == UNAUTHORIZED_TOOL_CALL

    @pytest.mark.parametrize("params", [None, {}, {"name": ""}, {"name": 42}])
    def test_a_broken_call_is_invalid_params_not_unauthorised(
        self, params: dict | None
    ) -> None:
        """A malformed message is not an access decision.

        Returning unauthorised here would tell a caller that a badly formed
        request was a permissions problem, which is both wrong and misleading.
        """
        decision = evaluate("tools/call", params, VIEWER)
        assert decision.allowed is False
        assert decision.error_code == INVALID_PARAMS


class TestRefusalLeaksNothing:
    def test_the_message_names_no_role_and_no_tool(self) -> None:
        decision = evaluate("tools/call", {"name": "admin_reset_key"}, VIEWER)
        message = decision.error_message or ""
        assert "viewer" not in message.lower()
        assert "admin_reset_key" not in message
        # The detail is kept for the operator, not the caller.
        assert "admin_reset_key" in decision.reason

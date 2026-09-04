"""HTTP level tests for the MCP security gateway.

The bridge is replaced with a stand in that records everything it is asked to
forward. That recording is what proves the central requirement: a refused
request never reaches the downstream server, rather than reaching it and having
its answer discarded.
"""

from typing import Any

import pytest
from fastapi.testclient import TestClient

from src.core.errors import (
    INTERNAL_ERROR,
    INVALID_REQUEST,
    PARSE_ERROR,
    UNAUTHENTICATED,
    UNAUTHORIZED_TOOL_CALL,
)
from src.mcp_gateway.stdio_bridge import BridgeError
from tests.test_auth import make_token

DOWNSTREAM_SECRET_PATH = "/opt/internal/mcp/server.py"


class RecordingBridge:
    """Stands in for the real bridge and remembers what it was asked to do."""

    def __init__(self) -> None:
        self.requests: list[tuple[str, dict[str, Any] | None]] = []
        self.notifications: list[str] = []
        self.initialize_result: dict[str, Any] | None = {
            "protocolVersion": "2025-06-18",
            "serverInfo": {"name": "stub", "version": "1.0.0"},
            "capabilities": {},
        }
        self.fail_with: Exception | None = None

    def is_running(self) -> bool:
        return True

    async def ensure_started(self) -> None:
        if self.fail_with is not None:
            raise self.fail_with

    async def stop(self) -> None:
        return None

    async def forward_request(
        self, client_id: Any, method: str, params: dict[str, Any] | None
    ) -> dict[str, Any]:
        if self.fail_with is not None:
            raise self.fail_with
        self.requests.append((method, params))
        return {
            "jsonrpc": "2.0",
            "id": client_id,
            "result": {"forwarded": method},
        }

    async def forward_notification(
        self, method: str, params: dict[str, Any] | None
    ) -> None:
        self.notifications.append(method)


@pytest.fixture()
def stub(monkeypatch: pytest.MonkeyPatch) -> RecordingBridge:
    recording = RecordingBridge()
    monkeypatch.setattr("src.mcp_gateway.app.bridge", recording)
    return recording


@pytest.fixture()
def client(stub: RecordingBridge) -> Any:
    from src.mcp_gateway.app import app

    with TestClient(app) as test_client:
        yield test_client


def viewer_headers() -> dict[str, str]:
    return {"Authorization": f"Bearer {make_token(role='viewer')}"}


def admin_headers() -> dict[str, str]:
    return {"Authorization": f"Bearer {make_token(role='admin')}"}


def call(name: str, request_id: int = 1) -> dict[str, Any]:
    return {
        "jsonrpc": "2.0",
        "id": request_id,
        "method": "tools/call",
        "params": {"name": name, "arguments": {}},
    }


class TestAuthentication:
    def test_no_token_is_rejected(self, client: Any, stub: RecordingBridge) -> None:
        response = client.post("/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
        assert response.status_code == 401
        assert response.json()["error"]["code"] == UNAUTHENTICATED
        assert stub.requests == []

    def test_a_bad_token_is_rejected(self, client: Any, stub: RecordingBridge) -> None:
        response = client.post(
            "/mcp",
            headers={"Authorization": "Bearer rubbish"},
            json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
        )
        assert response.status_code == 401
        assert stub.requests == []

    def test_the_rejection_says_nothing_useful(self, client: Any) -> None:
        response = client.post(
            "/mcp",
            headers={"Authorization": f"Bearer {make_token(minutes=-5)}"},
            json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
        )
        message = response.json()["error"]["message"].lower()
        assert "expired" not in message
        assert "signature" not in message


class TestTransparentForwarding:
    def test_viewer_tools_list_reaches_the_downstream(
        self, client: Any, stub: RecordingBridge
    ) -> None:
        response = client.post(
            "/mcp",
            headers=viewer_headers(),
            json={"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
        )
        assert response.status_code == 200
        assert response.json()["id"] == 2
        assert stub.requests == [("tools/list", None)]

    def test_viewer_may_call_an_ordinary_tool(
        self, client: Any, stub: RecordingBridge
    ) -> None:
        response = client.post(
            "/mcp", headers=viewer_headers(), json=call("get_customer_record", 3)
        )
        assert response.status_code == 200
        assert "error" not in response.json()
        assert len(stub.requests) == 1


class TestPrivilegedToolCalls:
    def test_viewer_is_blocked_before_the_downstream_is_touched(
        self, client: Any, stub: RecordingBridge
    ) -> None:
        """The requirement this whole task exists for.

        By the time a privileged tool has executed, blocking it is too late, so
        the assertion that matters is that nothing was forwarded at all.
        """
        response = client.post(
            "/mcp", headers=viewer_headers(), json=call("admin_reset_key", 4)
        )
        body = response.json()
        assert body["error"]["code"] == UNAUTHORIZED_TOOL_CALL
        assert body["error"]["message"] == "Unauthorized Tool Call"
        assert body["id"] == 4
        assert stub.requests == [], "a blocked call must never reach the downstream"

    def test_admin_is_forwarded(self, client: Any, stub: RecordingBridge) -> None:
        response = client.post(
            "/mcp", headers=admin_headers(), json=call("admin_reset_key", 5)
        )
        assert "error" not in response.json()
        assert stub.requests == [
            ("tools/call", {"name": "admin_reset_key", "arguments": {}})
        ]

    def test_case_variation_does_not_bypass_the_gateway(
        self, client: Any, stub: RecordingBridge
    ) -> None:
        response = client.post(
            "/mcp", headers=viewer_headers(), json=call("ADMIN_RESET_KEY", 6)
        )
        assert response.json()["error"]["code"] == UNAUTHORIZED_TOOL_CALL
        assert stub.requests == []


class TestMalformedRequests:
    def test_invalid_json_is_a_parse_error(self, client: Any) -> None:
        response = client.post(
            "/mcp",
            headers={**viewer_headers(), "Content-Type": "application/json"},
            content=b"{not json",
        )
        assert response.json()["error"]["code"] == PARSE_ERROR

    def test_batches_are_refused(self, client: Any, stub: RecordingBridge) -> None:
        response = client.post(
            "/mcp",
            headers=viewer_headers(),
            json=[{"jsonrpc": "2.0", "id": 1, "method": "tools/list"}],
        )
        assert response.json()["error"]["code"] == INVALID_REQUEST
        assert stub.requests == []

    def test_a_missing_method_is_an_invalid_request(self, client: Any) -> None:
        response = client.post(
            "/mcp", headers=viewer_headers(), json={"jsonrpc": "2.0", "id": 1}
        )
        assert response.json()["error"]["code"] == INVALID_REQUEST


class TestNotifications:
    def test_a_notification_gets_no_body(
        self, client: Any, stub: RecordingBridge
    ) -> None:
        """A JSON-RPC message with no id must not be answered."""
        response = client.post(
            "/mcp",
            headers=viewer_headers(),
            json={"jsonrpc": "2.0", "method": "notifications/initialized"},
        )
        assert response.status_code == 202
        assert response.content == b""


class TestUpstreamFailure:
    def test_downstream_failure_is_sanitised(
        self, client: Any, stub: RecordingBridge
    ) -> None:
        stub.fail_with = BridgeError(
            f"child process {DOWNSTREAM_SECRET_PATH} died with signal 9"
        )
        response = client.post(
            "/mcp", headers=viewer_headers(), json=call("get_customer_record", 7)
        )
        body = response.json()

        assert response.status_code == 502
        assert body["error"]["code"] == INTERNAL_ERROR
        # The caller learns nothing about what is running behind the gateway.
        assert DOWNSTREAM_SECRET_PATH not in response.text
        assert "BridgeError" not in response.text
        assert "signal 9" not in response.text
        # But the operator gets a handle to find the full story in the log.
        assert body["error"]["data"]["error_id"]


class TestHealth:
    def test_health_reports_downstream_state(self, client: Any) -> None:
        body = client.get("/health").json()
        assert body["status"] == "ok"
        assert body["downstream_running"] is True

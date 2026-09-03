"""End to end test of the MCP server over a real stdio pipe.

The server is started as a child process and driven with raw JSON-RPC, which is
the only way to prove the two things this task is scored on: that stdout carries
nothing but JSON-RPC, and that a malformed message produces a protocol error
while an impossible one produces an error flagged result.
"""

import json
import os
import queue
import subprocess
import sys
import threading
from pathlib import Path
from typing import Any

import pytest

try:
    from mcp.types import LATEST_PROTOCOL_VERSION as PROTOCOL_VERSION
except ImportError:  # pragma: no cover
    PROTOCOL_VERSION = "2025-06-18"

PROJECT_ROOT = Path(__file__).resolve().parents[1]
INVALID_PARAMS = -32602
METHOD_NOT_FOUND = -32601
READ_TIMEOUT_SECONDS = 20


class ServerSession:
    """Drives the server over stdin and stdout, capturing every byte of both."""

    def __init__(self) -> None:
        self.process = subprocess.Popen(
            [sys.executable, "-m", "src.mcp_server.server"],
            cwd=str(PROJECT_ROOT),
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            bufsize=1,
            # The child logs at INFO deliberately. A chatty server is a far
            # stronger test of stdout purity than a silent one, and the start up
            # line is what proves logging reaches stderr.
            env={**os.environ, "LOG_LEVEL": "INFO"},
        )
        self.stdout_lines: list[str] = []
        self.stderr_lines: list[str] = []
        self._inbox: queue.Queue[str | None] = queue.Queue()
        threading.Thread(target=self._drain_stdout, daemon=True).start()
        threading.Thread(target=self._drain_stderr, daemon=True).start()

    def _drain_stdout(self) -> None:
        assert self.process.stdout is not None
        for line in self.process.stdout:
            self.stdout_lines.append(line)
            self._inbox.put(line)
        self._inbox.put(None)

    def _drain_stderr(self) -> None:
        assert self.process.stderr is not None
        for line in self.process.stderr:
            self.stderr_lines.append(line)

    def send(self, message: dict[str, Any]) -> None:
        assert self.process.stdin is not None
        self.process.stdin.write(json.dumps(message) + "\n")
        self.process.stdin.flush()

    def await_response(self, request_id: int) -> dict[str, Any]:
        """Read until the reply with this id arrives, skipping notifications."""
        while True:
            try:
                line = self._inbox.get(timeout=READ_TIMEOUT_SECONDS)
            except queue.Empty:
                raise AssertionError(
                    f"no response to id {request_id} within "
                    f"{READ_TIMEOUT_SECONDS}s. stderr:\n"
                    + "".join(self.stderr_lines)
                )
            if line is None:
                raise AssertionError(
                    "server closed stdout before replying to id "
                    f"{request_id}. stderr:\n" + "".join(self.stderr_lines)
                )
            payload = json.loads(line)
            if payload.get("id") == request_id:
                return payload

    def handshake(self) -> None:
        self.send(
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {
                    "protocolVersion": PROTOCOL_VERSION,
                    "capabilities": {},
                    "clientInfo": {"name": "stdio-isolation-test", "version": "1.0.0"},
                },
            }
        )
        response = self.await_response(1)
        assert "result" in response, f"initialize failed: {response}"
        self.send({"jsonrpc": "2.0", "method": "notifications/initialized"})

    def call_tool(
        self, request_id: int, name: str, arguments: dict[str, Any]
    ) -> dict[str, Any]:
        self.send(
            {
                "jsonrpc": "2.0",
                "id": request_id,
                "method": "tools/call",
                "params": {"name": name, "arguments": arguments},
            }
        )
        return self.await_response(request_id)

    def close(self) -> None:
        assert self.process.stdin is not None
        self.process.stdin.close()
        try:
            self.process.wait(timeout=10)
        except subprocess.TimeoutExpired:  # pragma: no cover
            self.process.kill()
            self.process.wait(timeout=5)


@pytest.fixture(scope="module")
def session() -> Any:
    """One server process drives every assertion in this module."""
    server = ServerSession()
    server.handshake()

    results: dict[str, dict[str, Any]] = {}

    server.send({"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
    results["list"] = server.await_response(2)

    results["valid_lookup"] = server.call_tool(
        3, "get_customer_record", {"customer_id": "CUST-10001"}
    )
    results["malformed_id"] = server.call_tool(
        4, "get_customer_record", {"customer_id": "not-a-customer"}
    )
    results["unknown_customer"] = server.call_tool(
        5, "get_customer_record", {"customer_id": "CUST-99999"}
    )
    results["negative_amount"] = server.call_tool(
        6,
        "trigger_refund",
        {
            "customer_id": "CUST-10001",
            "amount": -50.0,
            "reason": "Attempting a negative refund",
        },
    )
    results["short_reason"] = server.call_tool(
        7,
        "trigger_refund",
        {"customer_id": "CUST-10001", "amount": 10.0, "reason": "damaged"},
    )
    results["unknown_tool"] = server.call_tool(8, "admin_reset_key", {})
    results["valid_refund"] = server.call_tool(
        9,
        "trigger_refund",
        {
            "customer_id": "CUST-10001",
            "amount": 12.75,
            "reason": "Order cancelled before dispatch",
        },
    )

    server.close()
    yield server, results


class TestStdioIsolation:
    def test_stdout_carries_nothing_but_json(self, session: Any) -> None:
        """The requirement this whole task is built around.

        A single stray print anywhere in the process, or in any library it
        imports, puts a non JSON line on this stream and fails here.
        """
        server, _ = session
        assert server.stdout_lines, "the server wrote nothing to stdout"
        for index, line in enumerate(server.stdout_lines):
            if not line.strip():
                continue
            try:
                parsed = json.loads(line)
            except json.JSONDecodeError:
                pytest.fail(
                    f"stdout line {index} is not valid JSON, which breaks the "
                    f"transport: {line!r}"
                )
            assert parsed.get("jsonrpc") == "2.0", (
                f"stdout line {index} is JSON but not a JSON-RPC message: {line!r}"
            )

    def test_logs_are_written_to_stderr(self, session: Any) -> None:
        server, _ = session
        combined = "".join(server.stderr_lines)
        assert "MCP server starting" in combined, (
            "the start up log line did not appear on stderr"
        )

    def test_server_exits_cleanly_on_end_of_input(self, session: Any) -> None:
        server, _ = session
        assert server.process.returncode == 0


class TestProtocolBehaviour:
    def test_tools_list_advertises_both_tools(self, session: Any) -> None:
        _, results = session
        names = {tool["name"] for tool in results["list"]["result"]["tools"]}
        assert names == {"get_customer_record", "trigger_refund"}

    def test_valid_lookup_succeeds(self, session: Any) -> None:
        _, results = session
        result = results["valid_lookup"]["result"]
        assert result["isError"] is False
        assert "CUST-10001" in result["content"][0]["text"]

    @pytest.mark.parametrize(
        "key", ["malformed_id", "negative_amount", "short_reason"]
    )
    def test_malformed_input_is_a_protocol_error(self, session: Any, key: str) -> None:
        """Schema failures come back as JSON-RPC errors, not as results."""
        _, results = session
        response = results[key]
        assert "error" in response, f"{key} should have produced a protocol error"
        assert response["error"]["code"] == INVALID_PARAMS
        assert "result" not in response

    def test_error_response_echoes_the_request_id(self, session: Any) -> None:
        _, results = session
        assert results["malformed_id"]["id"] == 4

    def test_unknown_tool_is_method_not_found(self, session: Any) -> None:
        _, results = session
        assert results["unknown_tool"]["error"]["code"] == METHOD_NOT_FOUND

    def test_unknown_customer_is_an_error_result_not_a_protocol_error(
        self, session: Any
    ) -> None:
        """The other half of the split.

        The message was perfectly well formed, so the model gets a readable
        result it can act on rather than a transport level fault.
        """
        _, results = session
        response = results["unknown_customer"]
        assert "error" not in response
        assert response["result"]["isError"] is True
        assert "CUST-99999" in response["result"]["content"][0]["text"]

    def test_valid_refund_is_issued(self, session: Any) -> None:
        _, results = session
        result = results["valid_refund"]["result"]
        assert result["isError"] is False
        assert "REF-" in result["content"][0]["text"]

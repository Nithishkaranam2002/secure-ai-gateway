"""Bridge between HTTP and the stdio MCP server.

The gateway speaks HTTP outward and the MCP server speaks stdio, so something
has to translate. This module owns the child process and the single pipe into
it, and solves the three problems that come with that.

A pipe is one lane. Concurrent callers must not interleave their writes, so
every write goes through one lock and one reader loop dispatches replies.

Ids collide. Two HTTP clients will both happily open with id 1, so the bridge
assigns its own internal id per request and restores the caller's id on the way
back out.

The session is initialised once but the callers are many. MCP servers accept a
handshake once, so the bridge performs it at startup, keeps the result, and
replays it to every client that sends its own initialize.
"""

import asyncio
import json
import os
import sys
from pathlib import Path
from typing import Any

from src.core.logging_setup import get_logger

try:
    from mcp.types import LATEST_PROTOCOL_VERSION as PROTOCOL_VERSION
except ImportError:  # pragma: no cover
    PROTOCOL_VERSION = "2025-06-18"

logger = get_logger(__name__)

PROJECT_ROOT = Path(__file__).resolve().parents[2]
SERVER_MODULE = "src.mcp_server.server"

# A tool result can be large. The default stream limit is 64 KB and a single
# oversized line would break the reader for every caller, not just its own.
READ_LIMIT = 4 * 1024 * 1024

DEFAULT_TIMEOUT_SECONDS = 30.0
HANDSHAKE_TIMEOUT_SECONDS = 20.0


class BridgeError(Exception):
    """The downstream server could not be reached or did not answer."""


class MCPStdioBridge:
    def __init__(self, timeout: float = DEFAULT_TIMEOUT_SECONDS) -> None:
        self._timeout = timeout
        self._process: asyncio.subprocess.Process | None = None
        self._pending: dict[int, asyncio.Future[dict[str, Any]]] = {}
        self._next_id = 0
        self._write_lock = asyncio.Lock()
        self._start_lock = asyncio.Lock()
        self._reader_task: asyncio.Task[None] | None = None
        self._stderr_task: asyncio.Task[None] | None = None
        self._ready = False
        self.initialize_result: dict[str, Any] | None = None

    # ---------------------------------------------------------------- lifecycle

    def is_running(self) -> bool:
        return self._process is not None and self._process.returncode is None

    async def ensure_started(self) -> None:
        if self._ready and self.is_running():
            return
        async with self._start_lock:
            if self._ready and self.is_running():
                return
            await self._spawn()
            await self._handshake()
            self._ready = True

    async def _spawn(self) -> None:
        logger.info("starting downstream MCP server as a child process")
        self._process = await asyncio.create_subprocess_exec(
            sys.executable,
            "-m",
            SERVER_MODULE,
            cwd=str(PROJECT_ROOT),
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=os.environ.copy(),
            limit=READ_LIMIT,
        )
        self._next_id = 0
        self._reader_task = asyncio.create_task(self._read_stdout())
        self._stderr_task = asyncio.create_task(self._read_stderr())

    async def _handshake(self) -> None:
        response = await self._request(
            "initialize",
            {
                "protocolVersion": PROTOCOL_VERSION,
                "capabilities": {},
                "clientInfo": {"name": "secure-ai-gateway", "version": "1.0.0"},
            },
            timeout=HANDSHAKE_TIMEOUT_SECONDS,
        )
        if "error" in response:
            raise BridgeError(f"downstream refused initialize: {response['error']}")
        self.initialize_result = response.get("result")
        await self._notify("notifications/initialized", None)
        logger.info("downstream MCP session initialised")

    async def stop(self) -> None:
        self._ready = False
        for task in (self._reader_task, self._stderr_task):
            if task is not None:
                task.cancel()
        self._reader_task = None
        self._stderr_task = None

        process = self._process
        self._process = None
        if process is None or process.returncode is not None:
            return

        if process.stdin is not None and not process.stdin.is_closing():
            process.stdin.close()
        try:
            await asyncio.wait_for(process.wait(), timeout=5.0)
        except asyncio.TimeoutError:  # pragma: no cover
            logger.warning("downstream did not exit, terminating")
            process.terminate()
            try:
                await asyncio.wait_for(process.wait(), timeout=5.0)
            except asyncio.TimeoutError:
                process.kill()
        logger.info("downstream MCP server stopped")

    # ------------------------------------------------------------------- pumps

    async def _read_stdout(self) -> None:
        process = self._process
        assert process is not None and process.stdout is not None
        try:
            while True:
                line = await process.stdout.readline()
                if not line:
                    break
                self._dispatch(line)
        except asyncio.CancelledError:  # pragma: no cover
            raise
        except Exception:  # pragma: no cover
            logger.exception("downstream stdout reader failed")
        finally:
            self._fail_pending("downstream closed its output stream")
            self._ready = False

    def _dispatch(self, raw: bytes) -> None:
        text = raw.decode("utf-8", errors="replace").strip()
        if not text:
            return
        try:
            payload = json.loads(text)
        except json.JSONDecodeError:
            # A non JSON line on the downstream stdout is exactly the failure
            # Task 1 guards against. Log it loudly rather than crash the pump.
            logger.error("downstream wrote a non JSON line to stdout: %r", text[:200])
            return

        message_id = payload.get("id")
        if message_id is None:
            logger.debug("downstream notification: %s", payload.get("method"))
            return

        future = self._pending.pop(message_id, None)
        if future is None:
            logger.warning("downstream reply with unmatched id %s", message_id)
            return
        if not future.done():
            future.set_result(payload)

    async def _read_stderr(self) -> None:
        """Forward the child's logs into ours so they are never lost."""
        process = self._process
        assert process is not None and process.stderr is not None
        try:
            while True:
                line = await process.stderr.readline()
                if not line:
                    break
                logger.info(
                    "downstream: %s", line.decode("utf-8", errors="replace").rstrip()
                )
        except asyncio.CancelledError:  # pragma: no cover
            raise
        except Exception:  # pragma: no cover
            logger.exception("downstream stderr reader failed")

    def _fail_pending(self, reason: str) -> None:
        for future in self._pending.values():
            if not future.done():
                future.set_exception(BridgeError(reason))
        self._pending.clear()

    # ------------------------------------------------------------------ sending

    def _allocate_id(self) -> int:
        self._next_id += 1
        return self._next_id

    async def _write(self, message: dict[str, Any]) -> None:
        process = self._process
        if process is None or process.stdin is None or process.returncode is not None:
            raise BridgeError("downstream server is not running")
        encoded = (json.dumps(message) + "\n").encode("utf-8")
        async with self._write_lock:
            process.stdin.write(encoded)
            await process.stdin.drain()

    async def _request(
        self,
        method: str,
        params: dict[str, Any] | None,
        timeout: float | None = None,
    ) -> dict[str, Any]:
        internal_id = self._allocate_id()
        future: asyncio.Future[dict[str, Any]] = asyncio.get_running_loop().create_future()
        self._pending[internal_id] = future

        message: dict[str, Any] = {
            "jsonrpc": "2.0",
            "id": internal_id,
            "method": method,
        }
        if params is not None:
            message["params"] = params

        try:
            await self._write(message)
            return await asyncio.wait_for(future, timeout or self._timeout)
        except asyncio.TimeoutError as exc:
            raise BridgeError(
                f"downstream did not respond to {method} in time"
            ) from exc
        finally:
            self._pending.pop(internal_id, None)

    async def _notify(self, method: str, params: dict[str, Any] | None) -> None:
        message: dict[str, Any] = {"jsonrpc": "2.0", "method": method}
        if params is not None:
            message["params"] = params
        await self._write(message)

    # -------------------------------------------------------------- public API

    async def forward_request(
        self,
        client_id: Any,
        method: str,
        params: dict[str, Any] | None,
    ) -> dict[str, Any]:
        """Send a request downstream and return the reply under the caller's id."""
        await self.ensure_started()
        response = await self._request(method, params)
        response["id"] = client_id
        return response

    async def forward_notification(
        self, method: str, params: dict[str, Any] | None
    ) -> None:
        await self.ensure_started()
        await self._notify(method, params)


bridge = MCPStdioBridge()

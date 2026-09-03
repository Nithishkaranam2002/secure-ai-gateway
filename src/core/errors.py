import uuid
from typing import Any

# JSON-RPC 2.0 standard codes
PARSE_ERROR = -32700
INVALID_REQUEST = -32600
METHOD_NOT_FOUND = -32601
INVALID_PARAMS = -32602
INTERNAL_ERROR = -32603

# Application defined code, reserved range -32000 to -32099
UNAUTHORIZED_TOOL_CALL = -32001


def new_error_id() -> str:
    """Short id shared between the public error and the internal log entry."""
    return uuid.uuid4().hex[:12]


class GatewayError(Exception):
    """An error that is safe to show to a caller.

    message is what the caller sees. internal_detail never leaves the process
    and is written to the log and the audit table under the same error_id.
    """

    def __init__(
        self,
        status_code: int,
        code: str,
        message: str,
        internal_detail: str = "",
        error_id: str | None = None,
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.code = code
        self.message = message
        self.internal_detail = internal_detail
        self.error_id = error_id or new_error_id()

    def public_payload(self) -> dict[str, Any]:
        return {
            "error": {
                "code": self.code,
                "message": self.message,
                "error_id": self.error_id,
            }
        }


def sanitise(exc: Exception) -> GatewayError:
    """Turn any unexpected exception into a safe outward facing error."""
    if isinstance(exc, GatewayError):
        return exc
    return GatewayError(
        status_code=502,
        code="upstream_error",
        message="The request could not be completed.",
        internal_detail=f"{type(exc).__name__}: {exc}",
    )


def jsonrpc_error(
    request_id: Any, code: int, message: str, data: Any = None
) -> dict[str, Any]:
    """Build a JSON-RPC 2.0 error response, echoing the caller's id."""
    error: dict[str, Any] = {"code": code, "message": message}
    if data is not None:
        error["data"] = data
    return {"jsonrpc": "2.0", "id": request_id, "error": error}

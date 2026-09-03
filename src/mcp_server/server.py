"""MCP server over stdio transport.

This is the only module in the MCP half of the project that knows about
JSON-RPC. It has two jobs and keeps them apart deliberately.

A malformed message is a protocol failure. It is signalled by raising, because
an exception from a low level handler becomes a JSON-RPC error response, which
is what -32602 invalid params means.

A well formed request that cannot be completed is not a protocol failure. It is
returned as a normal tool result with isError set, so the calling model can read
the explanation and correct itself rather than seeing the connection fault.

stdout carries JSON-RPC only. Every log line in this process goes to stderr via
src/core/logging_setup.
"""

import asyncio
import json

from mcp import MCPError
from mcp.server import Server, ServerRequestContext
from mcp.server.stdio import stdio_server
from mcp.types import (
    INTERNAL_ERROR,
    INVALID_PARAMS,
    METHOD_NOT_FOUND,
    CallToolRequestParams,
    CallToolResult,
    ListToolsResult,
    PaginatedRequestParams,
    TextContent,
    Tool,
)
from pydantic import BaseModel, ValidationError

from src.core.database import initialise_database
from src.core.logging_setup import get_logger
from src.mcp_server import tools
from src.mcp_server.schemas import (
    GetCustomerRecordInput,
    TriggerRefundInput,
    json_schema,
)

logger = get_logger(__name__)

SERVER_NAME = "secure-ai-gateway-mcp"
SERVER_VERSION = "1.0.0"

# The model never reads this source file. The description below is the only
# thing it uses to decide whether a tool fits the situation, so it states both
# what the tool does and when not to reach for it.
TOOL_DEFINITIONS: list[Tool] = [
    Tool(
        name="get_customer_record",
        title="Get customer record",
        description=(
            "Look up a single customer by their identifier and return their "
            "name, email, plan and account status. Read only. Use this before "
            "any action that depends on who the customer is or whether their "
            "account is active."
        ),
        inputSchema=json_schema(GetCustomerRecordInput),
    ),
    Tool(
        name="trigger_refund",
        title="Trigger refund",
        description=(
            "Issue a refund to a customer and record it. This moves real money "
            "and cannot be undone from here. Only call it when the customer has "
            "asked for a refund and you can state a specific reason. Do not call "
            "it to check whether a refund is possible."
        ),
        inputSchema=json_schema(TriggerRefundInput),
    ),
]

TOOL_SCHEMAS: dict[str, type[BaseModel]] = {
    "get_customer_record": GetCustomerRecordInput,
    "trigger_refund": TriggerRefundInput,
}


def _describe_validation_error(exc: ValidationError) -> str:
    """Turn a Pydantic failure into one readable line naming every bad field.

    The model has to be able to fix its own call from this string, so it names
    the field and what was wrong rather than saying the input was invalid.
    """
    parts: list[str] = []
    for error in exc.errors():
        location = ".".join(str(item) for item in error["loc"]) or "arguments"
        parts.append(f"{location}: {error['msg']}")
    return "; ".join(parts) if parts else "arguments failed validation"


async def on_list_tools(
    context: ServerRequestContext,
    params: PaginatedRequestParams | None,
) -> ListToolsResult:
    logger.info("tools/list served, %d tools", len(TOOL_DEFINITIONS))
    return ListToolsResult(tools=TOOL_DEFINITIONS)


async def on_call_tool(
    context: ServerRequestContext,
    params: CallToolRequestParams,
) -> CallToolResult:
    name = params.name
    arguments = params.arguments or {}

    schema = TOOL_SCHEMAS.get(name)
    if schema is None:
        logger.warning("tools/call for unknown tool %s", name)
        raise MCPError(METHOD_NOT_FOUND, f"Unknown tool: {name}")

    # Protocol level. A message that does not match the advertised schema never
    # reaches the business logic.
    try:
        validated = schema.model_validate(arguments)
    except ValidationError as exc:
        detail = _describe_validation_error(exc)
        logger.warning("tools/call %s rejected at validation: %s", name, detail)
        raise MCPError(INVALID_PARAMS, detail) from exc

    # Business level. Everything from here is a well formed request.
    try:
        if name == "get_customer_record":
            assert isinstance(validated, GetCustomerRecordInput)
            payload = tools.get_customer_record(validated.customer_id)
        else:
            assert isinstance(validated, TriggerRefundInput)
            payload = tools.trigger_refund(
                customer_id=validated.customer_id,
                amount=float(validated.amount),
                reason=validated.reason,
            )
    except tools.ToolExecutionError as exc:
        logger.info("tools/call %s could not complete: %s", name, exc.code)
        return CallToolResult(
            content=[TextContent(type="text", text=exc.message)],
            isError=True,
        )
    except Exception as exc:
        # Genuine fault inside the server. The caller gets a flat message with
        # no internals; the detail stays in the log.
        logger.exception("unhandled failure in tool %s", name)
        raise MCPError(INTERNAL_ERROR, "The tool could not be completed.") from exc

    logger.info("tools/call %s completed", name)
    return CallToolResult(
        content=[TextContent(type="text", text=json.dumps(payload, indent=2))],
        isError=False,
    )


def build_server() -> Server:
    return Server(
        name=SERVER_NAME,
        version=SERVER_VERSION,
        title="Secure AI Gateway MCP Server",
        instructions=(
            "Customer support tools for looking up customers and issuing "
            "refunds. Always look up the customer before issuing a refund."
        ),
        on_list_tools=on_list_tools,
        on_call_tool=on_call_tool,
    )


async def main() -> None:
    initialise_database()
    server = build_server()
    logger.info("MCP server starting on stdio transport")
    async with stdio_server() as (read_stream, write_stream):
        await server.run(
            read_stream,
            write_stream,
            server.create_initialization_options(),
        )


if __name__ == "__main__":
    asyncio.run(main())

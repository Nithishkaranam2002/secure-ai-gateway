"""Which requests the gateway allows through.

The whole rule set for Task 2 lives here, separate from transport and from
authentication, so it can be read and tested on its own.
"""

from dataclasses import dataclass
from typing import Any

from src.core.errors import INVALID_PARAMS, UNAUTHORIZED_TOOL_CALL
from src.core.logging_setup import get_logger
from src.core.policy_config import policy as loaded_policy
from src.mcp_gateway.auth import Principal

logger = get_logger(__name__)

# Rules come from config/policy.yaml so a deployment with different tool names
# or roles is a configuration change rather than a release.
ADMIN_TOOL_PREFIX = (
    loaded_policy.privileged_prefixes[0].prefix
    if loaded_policy.privileged_prefixes
    else "admin_"
)

TRANSPARENT_METHODS = loaded_policy.transparent_methods

UNAUTHORIZED_MESSAGE = "Unauthorized Tool Call"


@dataclass(frozen=True)
class Decision:
    """The outcome of evaluating one request."""

    allowed: bool
    action: str
    tool_name: str | None = None
    error_code: int | None = None
    error_message: str | None = None
    reason: str = ""


def is_admin_tool(tool_name: str) -> bool:
    """True if the tool name is in the privileged namespace.

    The name is normalised before comparison. Comparing the raw string would
    let `ADMIN_reset_key` past a check for `admin_` while still reaching a
    downstream server that resolves the name case insensitively.
    """
    return tool_name.strip().casefold().startswith(ADMIN_TOOL_PREFIX)


def evaluate(
    method: str,
    params: dict[str, Any] | None,
    principal: Principal,
) -> Decision:
    if method in TRANSPARENT_METHODS:
        return Decision(allowed=True, action=method, reason="transparent method")

    if method != "tools/call":
        # Anything else the protocol carries is forwarded. This gateway governs
        # tool execution, not the rest of the protocol surface.
        return Decision(allowed=True, action=method, reason="forwarded method")

    tool_name = (params or {}).get("name")
    if not isinstance(tool_name, str) or not tool_name.strip():
        # A broken message is not an access decision. It is malformed input, and
        # it gets the code that means malformed input.
        return Decision(
            allowed=False,
            action="tools/call",
            error_code=INVALID_PARAMS,
            error_message="params.name is required and must be a non empty string",
            reason="tools/call without a usable tool name",
        )

    rule = loaded_policy.rule_for(tool_name)

    if rule.required_role == principal.role or principal.is_admin:
        return Decision(
            allowed=True,
            action="tools/call",
            tool_name=tool_name,
            reason=f"role {principal.role} satisfies required role {rule.required_role}",
        )

    # The outward message is the flat phrase from the brief and nothing more.
    # Saying which role was required, or that the tool exists, would tell a
    # caller how the permission model is shaped.
    return Decision(
        allowed=False,
        action="tools/call",
        tool_name=tool_name,
        error_code=UNAUTHORIZED_TOOL_CALL,
        error_message=UNAUTHORIZED_MESSAGE,
        reason=f"role {principal.role} may not call privileged tool {tool_name}",
    )

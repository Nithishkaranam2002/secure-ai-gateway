"""Loads deployment policy from config/policy.yaml.

Tool names, required roles, redaction behaviour and provider limits differ per
customer. Keeping them in configuration means a new deployment is a file edit
rather than a code change, which is the difference between shipping a product
and shipping a demo.

The file is read once at import. If it is missing or malformed the built in
defaults below apply, because a gateway that will not start because of a
configuration typo is worse than one that starts with known safe rules and says
so loudly.
"""

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from src.core.logging_setup import get_logger

logger = get_logger(__name__)

PROJECT_ROOT = Path(__file__).resolve().parents[2]
POLICY_PATH = PROJECT_ROOT / "config" / "policy.yaml"

DEFAULTS: dict[str, Any] = {
    "roles": ["admin", "viewer"],
    "transparent_methods": [
        "initialize",
        "notifications/initialized",
        "ping",
        "tools/list",
    ],
    "tools": {
        "privileged_prefixes": [{"prefix": "admin_", "required_role": "admin"}],
        "overrides": {},
        "default": {"required_role": "viewer", "redact_result": True},
    },
    "redaction": {"enabled": True, "patterns": ["email", "ssn", "credit_card"]},
    "limits": {
        "default_tokens_per_minute": 50000,
        "window_seconds": 60,
        "minimum_charge_tokens": 100,
    },
    "providers": {
        "timeout_ms": 3000,
        "max_failover_hops": 1,
        "retry_on_status": [429, 500, 502, 503, 504],
    },
}


@dataclass(frozen=True)
class PrivilegedPrefix:
    prefix: str
    required_role: str


@dataclass(frozen=True)
class ToolRule:
    required_role: str
    redact_result: bool


@dataclass(frozen=True)
class Policy:
    roles: frozenset[str]
    transparent_methods: frozenset[str]
    privileged_prefixes: tuple[PrivilegedPrefix, ...]
    overrides: dict[str, ToolRule] = field(default_factory=dict)
    default_rule: ToolRule = ToolRule("viewer", True)
    redaction_enabled: bool = True
    source: str = "defaults"

    def rule_for(self, tool_name: str) -> ToolRule:
        """The rule governing one tool.

        Explicit overrides win, then prefix rules, then the default. The name is
        normalised before matching, because comparing the raw string would let
        `ADMIN_reset_key` past a check written for `admin_` while still reaching
        a downstream server that resolves names case insensitively.
        """
        normalised = tool_name.strip().casefold()

        override = self.overrides.get(normalised)
        if override is not None:
            return override

        for entry in self.privileged_prefixes:
            if normalised.startswith(entry.prefix.casefold()):
                return ToolRule(
                    required_role=entry.required_role,
                    redact_result=self.default_rule.redact_result,
                )

        return self.default_rule


def _rule_from(raw: dict[str, Any], fallback: ToolRule) -> ToolRule:
    return ToolRule(
        required_role=str(raw.get("required_role", fallback.required_role)),
        redact_result=bool(raw.get("redact_result", fallback.redact_result)),
    )


def load_policy(path: Path | None = None) -> Policy:
    target = path or POLICY_PATH
    source = str(target)

    try:
        raw = yaml.safe_load(target.read_text()) or {}
        if not isinstance(raw, dict):
            raise ValueError("policy file is not a mapping")
    except FileNotFoundError:
        logger.warning("no policy file at %s, using built in defaults", target)
        raw, source = DEFAULTS, "defaults (file not found)"
    except Exception as exc:
        # Starting with known safe rules beats refusing to start over a typo,
        # but it must be loud rather than silent.
        logger.error("policy file at %s could not be read (%s), using defaults", target, exc)
        raw, source = DEFAULTS, "defaults (file invalid)"

    tools = raw.get("tools") or DEFAULTS["tools"]
    default_rule = _rule_from(
        tools.get("default") or {}, ToolRule("viewer", True)
    )

    prefixes = tuple(
        PrivilegedPrefix(
            prefix=str(entry["prefix"]),
            required_role=str(entry.get("required_role", "admin")),
        )
        for entry in (tools.get("privileged_prefixes") or [])
        if isinstance(entry, dict) and entry.get("prefix")
    )

    overrides = {
        str(name).strip().casefold(): _rule_from(rule or {}, default_rule)
        for name, rule in (tools.get("overrides") or {}).items()
    }

    redaction = raw.get("redaction") or {}

    policy = Policy(
        roles=frozenset(str(r) for r in (raw.get("roles") or DEFAULTS["roles"])),
        transparent_methods=frozenset(
            str(m) for m in (raw.get("transparent_methods") or DEFAULTS["transparent_methods"])
        ),
        privileged_prefixes=prefixes,
        overrides=overrides,
        default_rule=default_rule,
        redaction_enabled=bool(redaction.get("enabled", True)),
        source=source,
    )

    if policy.source.startswith("defaults"):
        # Falling back is safe but it must never be quiet. A deployment running
        # on built in defaults while an operator believes their configuration is
        # in force is worse than one that never had a file.
        logger.warning(
            "POLICY FALLBACK: running on built in defaults (%s). "
            "The file at %s was not loaded.",
            policy.source,
            target,
        )
    else:
        logger.info(
            "policy loaded from %s: %d roles, %d privileged prefixes, %d overrides",
            policy.source,
            len(policy.roles),
            len(policy.privileged_prefixes),
            len(policy.overrides),
        )
    return policy


policy = load_policy()

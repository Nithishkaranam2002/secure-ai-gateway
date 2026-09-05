"""Tests for policy loaded from configuration.

The point of moving policy out of code is that a deployment with different tool
names or roles becomes a file edit. These tests prove that editing the file
actually changes the decision, rather than the file being decoration over
behaviour that is still hardcoded.
"""

from pathlib import Path

import pytest
import yaml

from src.core.policy_config import DEFAULTS, ToolRule, load_policy


def write_policy(tmp_path: Path, content: dict) -> Path:
    target = tmp_path / "policy.yaml"
    target.write_text(yaml.safe_dump(content))
    return target


class TestTheShippedFileLoads:
    def test_the_real_policy_file_parses(self) -> None:
        loaded = load_policy()
        assert "admin" in loaded.roles
        assert "tools/list" in loaded.transparent_methods
        assert loaded.privileged_prefixes

    def test_the_shipped_file_matches_the_brief(self) -> None:
        """The default deployment must still satisfy Task 2 as written."""
        loaded = load_policy()
        assert loaded.rule_for("admin_reset_key").required_role == "admin"
        assert loaded.rule_for("get_customer_record").required_role == "viewer"


class TestPrefixRules:
    def test_case_and_padding_are_not_a_bypass(self, tmp_path: Path) -> None:
        policy = load_policy(
            write_policy(
                tmp_path,
                {
                    "tools": {
                        "privileged_prefixes": [
                            {"prefix": "admin_", "required_role": "admin"}
                        ],
                        "default": {"required_role": "viewer", "redact_result": True},
                    }
                },
            )
        )
        for name in ["ADMIN_RESET_KEY", "  admin_reset_key  ", "Admin_Reset_Key"]:
            assert policy.rule_for(name).required_role == "admin"

    def test_a_renamed_privileged_namespace_works(self, tmp_path: Path) -> None:
        """The whole reason this is configuration.

        A customer whose privileged tools are named internal_* gets protection
        with no code change, and admin_* is no longer special for them.
        """
        policy = load_policy(
            write_policy(
                tmp_path,
                {
                    "tools": {
                        "privileged_prefixes": [
                            {"prefix": "internal_", "required_role": "admin"}
                        ],
                        "default": {"required_role": "viewer", "redact_result": True},
                    }
                },
            )
        )
        assert policy.rule_for("internal_reset_key").required_role == "admin"
        assert policy.rule_for("admin_reset_key").required_role == "viewer"

    def test_several_prefixes_are_supported(self, tmp_path: Path) -> None:
        policy = load_policy(
            write_policy(
                tmp_path,
                {
                    "tools": {
                        "privileged_prefixes": [
                            {"prefix": "admin_", "required_role": "admin"},
                            {"prefix": "internal_", "required_role": "admin"},
                        ],
                        "default": {"required_role": "viewer", "redact_result": True},
                    }
                },
            )
        )
        assert policy.rule_for("admin_x").required_role == "admin"
        assert policy.rule_for("internal_y").required_role == "admin"
        assert policy.rule_for("get_customer_record").required_role == "viewer"


class TestOverrides:
    def test_an_override_beats_a_prefix_rule(self, tmp_path: Path) -> None:
        policy = load_policy(
            write_policy(
                tmp_path,
                {
                    "tools": {
                        "privileged_prefixes": [
                            {"prefix": "admin_", "required_role": "admin"}
                        ],
                        "overrides": {
                            "admin_read_status": {
                                "required_role": "viewer",
                                "redact_result": True,
                            }
                        },
                        "default": {"required_role": "viewer", "redact_result": True},
                    }
                },
            )
        )
        assert policy.rule_for("admin_read_status").required_role == "viewer"
        assert policy.rule_for("admin_reset_key").required_role == "admin"

    def test_an_override_can_raise_a_requirement(self, tmp_path: Path) -> None:
        """A tool with an ordinary name that is nonetheless dangerous."""
        policy = load_policy(
            write_policy(
                tmp_path,
                {
                    "tools": {
                        "privileged_prefixes": [],
                        "overrides": {
                            "trigger_refund": {
                                "required_role": "admin",
                                "redact_result": True,
                            }
                        },
                        "default": {"required_role": "viewer", "redact_result": True},
                    }
                },
            )
        )
        assert policy.rule_for("trigger_refund").required_role == "admin"

    def test_overrides_are_matched_case_insensitively(self, tmp_path: Path) -> None:
        policy = load_policy(
            write_policy(
                tmp_path,
                {
                    "tools": {
                        "privileged_prefixes": [],
                        "overrides": {
                            "Trigger_Refund": {
                                "required_role": "admin",
                                "redact_result": True,
                            }
                        },
                        "default": {"required_role": "viewer", "redact_result": True},
                    }
                },
            )
        )
        assert policy.rule_for("trigger_refund").required_role == "admin"


class TestBadConfiguration:
    def test_a_missing_file_falls_back_to_safe_defaults(self, tmp_path: Path) -> None:
        """A gateway that refuses to start over a missing file is worse than one
        that starts with known safe rules and says so."""
        policy = load_policy(tmp_path / "does-not-exist.yaml")
        assert policy.rule_for("admin_reset_key").required_role == "admin"
        assert "not found" in policy.source

    def test_malformed_yaml_falls_back_to_safe_defaults(self, tmp_path: Path) -> None:
        broken = tmp_path / "policy.yaml"
        broken.write_text("tools: [this is not: valid: yaml: at all")
        policy = load_policy(broken)
        assert policy.rule_for("admin_reset_key").required_role == "admin"
        assert "invalid" in policy.source

    def test_a_file_that_is_not_a_mapping_falls_back(self, tmp_path: Path) -> None:
        broken = tmp_path / "policy.yaml"
        broken.write_text("- just\n- a\n- list\n")
        policy = load_policy(broken)
        assert policy.rule_for("admin_reset_key").required_role == "admin"

    def test_an_empty_file_still_protects_privileged_tools(self, tmp_path: Path) -> None:
        """The dangerous failure would be silently allowing everything."""
        empty = tmp_path / "policy.yaml"
        empty.write_text("")
        policy = load_policy(empty)
        assert policy.rule_for("admin_reset_key").required_role == "admin"

    def test_entries_missing_a_prefix_are_skipped_not_fatal(self, tmp_path: Path) -> None:
        policy = load_policy(
            write_policy(
                tmp_path,
                {
                    "tools": {
                        "privileged_prefixes": [
                            {"required_role": "admin"},
                            {"prefix": "admin_", "required_role": "admin"},
                        ],
                        "default": {"required_role": "viewer", "redact_result": True},
                    }
                },
            )
        )
        assert len(policy.privileged_prefixes) == 1
        assert policy.rule_for("admin_reset_key").required_role == "admin"


class TestDefaults:
    def test_the_built_in_defaults_protect_admin_tools(self) -> None:
        prefixes = DEFAULTS["tools"]["privileged_prefixes"]
        assert any(entry["prefix"] == "admin_" for entry in prefixes)

    def test_an_unlisted_tool_gets_the_default_rule(self, tmp_path: Path) -> None:
        policy = load_policy(
            write_policy(
                tmp_path,
                {
                    "tools": {
                        "privileged_prefixes": [],
                        "default": {"required_role": "viewer", "redact_result": False},
                    }
                },
            )
        )
        rule = policy.rule_for("some_new_tool")
        assert rule == ToolRule(required_role="viewer", redact_result=False)

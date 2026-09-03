"""Tests for the tool logic, with no protocol involved."""

import pytest

from src.mcp_server.tools import (
    MAX_AUTOMATED_REFUND,
    ToolExecutionError,
    get_customer_record,
    trigger_refund,
)


class TestGetCustomerRecord:
    def test_returns_a_known_customer(self) -> None:
        record = get_customer_record("CUST-10001")
        assert record["customer_id"] == "CUST-10001"
        assert record["status"] == "active"
        assert "email" in record

    def test_unknown_customer_is_a_business_failure(self) -> None:
        # Well formed request, impossible outcome. This must not be a protocol
        # error, because the model should be able to read it and move on.
        with pytest.raises(ToolExecutionError) as excinfo:
            get_customer_record("CUST-99999")
        assert excinfo.value.code == "customer_not_found"


class TestTriggerRefund:
    def test_issues_a_refund_for_an_active_customer(self) -> None:
        result = trigger_refund("CUST-10002", 25.50, "Item never arrived at all")
        assert result["status"] == "issued"
        assert result["amount"] == 25.50
        assert result["refund_id"].startswith("REF-")

    def test_refuses_a_suspended_customer(self) -> None:
        with pytest.raises(ToolExecutionError) as excinfo:
            trigger_refund("CUST-10004", 10.00, "Requested by the customer")
        assert excinfo.value.code == "customer_not_active"

    def test_refuses_a_closed_customer(self) -> None:
        with pytest.raises(ToolExecutionError) as excinfo:
            trigger_refund("CUST-10005", 10.00, "Requested by the customer")
        assert excinfo.value.code == "customer_not_active"

    def test_refuses_an_unknown_customer(self) -> None:
        with pytest.raises(ToolExecutionError) as excinfo:
            trigger_refund("CUST-99999", 10.00, "Requested by the customer")
        assert excinfo.value.code == "customer_not_found"

    def test_refuses_above_the_automated_ceiling(self) -> None:
        # A well formed request an agent should not be able to make alone.
        with pytest.raises(ToolExecutionError) as excinfo:
            trigger_refund(
                "CUST-10001",
                MAX_AUTOMATED_REFUND + 0.01,
                "Large refund requested by the customer",
            )
        assert excinfo.value.code == "refund_ceiling_exceeded"

    def test_allows_exactly_the_ceiling(self) -> None:
        result = trigger_refund(
            "CUST-10003",
            MAX_AUTOMATED_REFUND,
            "Enterprise contract cancellation",
        )
        assert result["status"] == "issued"

"""Validation tests for the tool input schemas."""

import pytest
from pydantic import ValidationError

from src.mcp_server.schemas import GetCustomerRecordInput, TriggerRefundInput


class TestCustomerIdFormat:
    def test_accepts_the_documented_format(self) -> None:
        model = GetCustomerRecordInput(customer_id="CUST-10001")
        assert model.customer_id == "CUST-10001"

    @pytest.mark.parametrize(
        "value",
        [
            "CUST-100",          # too few digits
            "CUST-1000123",      # too many digits
            "cust-10001",        # lowercase prefix
            "CUST10001",         # missing separator
            "10001",             # no prefix
            "CUST-ABCDE",        # letters where digits are required
            "CUST-1000 ",        # trailing space
            " CUST-10001",       # leading space
            "",                  # empty
            "CUST-10001\n",      # trailing newline
        ],
    )
    def test_rejects_malformed_ids(self, value: str) -> None:
        with pytest.raises(ValidationError):
            GetCustomerRecordInput(customer_id=value)

    def test_rejects_a_non_string(self) -> None:
        with pytest.raises(ValidationError):
            GetCustomerRecordInput(customer_id=10001)

    def test_rejects_unknown_fields(self) -> None:
        # A caller that misspells a field must not have it silently ignored.
        with pytest.raises(ValidationError):
            GetCustomerRecordInput(customer_id="CUST-10001", region="eu")

    def test_rejects_a_missing_field(self) -> None:
        with pytest.raises(ValidationError):
            GetCustomerRecordInput()


class TestRefundInput:
    def test_accepts_a_valid_refund(self) -> None:
        model = TriggerRefundInput(
            customer_id="CUST-10001",
            amount=49.99,
            reason="Package arrived damaged",
        )
        assert model.amount == 49.99

    def test_accepts_a_whole_number_amount(self) -> None:
        model = TriggerRefundInput(
            customer_id="CUST-10001",
            amount=50,
            reason="Duplicate charge on the account",
        )
        assert float(model.amount) == 50.0

    @pytest.mark.parametrize("amount", [0, -1, -49.99, 0.0])
    def test_rejects_non_positive_amounts(self, amount: float) -> None:
        with pytest.raises(ValidationError):
            TriggerRefundInput(
                customer_id="CUST-10001",
                amount=amount,
                reason="Package arrived damaged",
            )

    def test_rejects_an_amount_sent_as_text(self) -> None:
        # Money is not a place to accept a helpful type conversion.
        with pytest.raises(ValidationError):
            TriggerRefundInput(
                customer_id="CUST-10001",
                amount="49.99",
                reason="Package arrived damaged",
            )

    def test_rejects_sub_cent_precision(self) -> None:
        with pytest.raises(ValidationError):
            TriggerRefundInput(
                customer_id="CUST-10001",
                amount=49.999,
                reason="Package arrived damaged",
            )

    @pytest.mark.parametrize(
        "reason",
        [
            "damaged",             # nine characters or fewer
            "",                    # empty
            "          ",          # whitespace only, long enough to pass a naive check
            "  short  ",           # trims below the minimum
        ],
    )
    def test_rejects_thin_reasons(self, reason: str) -> None:
        with pytest.raises(ValidationError):
            TriggerRefundInput(
                customer_id="CUST-10001",
                amount=49.99,
                reason=reason,
            )

    def test_rejects_a_misspelled_field(self) -> None:
        with pytest.raises(ValidationError):
            TriggerRefundInput(
                customer_id="CUST-10001",
                ammount=49.99,
                reason="Package arrived damaged",
            )

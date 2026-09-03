"""Input schemas for the MCP tools.

These models are the single source of truth. They validate incoming tool
arguments, and their generated JSON schema is what the server advertises in
tools/list, so the advertised contract and the enforced contract cannot drift
apart.
"""

import math
import re
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, StrictFloat, StrictInt, field_validator

# Documented choice: the brief writes the format as CUST-XXXXX without saying
# whether the five characters are digits or letters. We require five digits and
# record the decision in docs/decisions.md.
CUSTOMER_ID_PATTERN = re.compile(r"^CUST-\d{5}$")

MIN_REASON_LENGTH = 10


class GetCustomerRecordInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    customer_id: str = Field(
        ...,
        description="Customer identifier in the format CUST-12345.",
    )

    @field_validator("customer_id")
    @classmethod
    def validate_customer_id(cls, value: str) -> str:
        if not CUSTOMER_ID_PATTERN.fullmatch(value):
            raise ValueError(
                "customer_id must match the format CUST-12345, "
                "meaning the literal prefix CUST- followed by exactly five digits"
            )
        return value


class TriggerRefundInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    customer_id: str = Field(
        ...,
        description="Customer identifier in the format CUST-12345.",
    )
    # StrictFloat | StrictInt accepts 49.99 and 50 but rejects the string "50".
    # A refund is a money movement, so a caller that cannot send a number is a
    # caller we do not trust to have meant the right amount.
    amount: StrictFloat | StrictInt = Field(
        ...,
        gt=0,
        description="Refund amount in the account currency. Must be greater than zero.",
    )
    reason: str = Field(
        ...,
        min_length=MIN_REASON_LENGTH,
        description=(
            "Why the refund is being issued. At least "
            f"{MIN_REASON_LENGTH} characters, so the audit trail is meaningful."
        ),
    )

    @field_validator("customer_id")
    @classmethod
    def validate_customer_id(cls, value: str) -> str:
        if not CUSTOMER_ID_PATTERN.fullmatch(value):
            raise ValueError(
                "customer_id must match the format CUST-12345, "
                "meaning the literal prefix CUST- followed by exactly five digits"
            )
        return value

    @field_validator("amount")
    @classmethod
    def validate_amount(cls, value: float | int) -> float:
        numeric = float(value)
        if not math.isfinite(numeric):
            raise ValueError("amount must be a finite number")
        if round(numeric, 2) != numeric:
            raise ValueError("amount must have at most two decimal places")
        return numeric

    @field_validator("reason")
    @classmethod
    def validate_reason(cls, value: str) -> str:
        # min_length alone would accept ten spaces, which is not a reason.
        if len(value.strip()) < MIN_REASON_LENGTH:
            raise ValueError(
                f"reason must contain at least {MIN_REASON_LENGTH} "
                "non whitespace characters"
            )
        return value


def json_schema(model: type[BaseModel]) -> dict[str, Any]:
    """JSON schema for a model, in the shape MCP expects for a tool input."""
    schema = model.model_json_schema()
    schema.pop("title", None)
    return schema

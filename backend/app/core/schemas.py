from datetime import datetime, timezone
from typing import Annotated, Literal
import uuid
from pydantic import BaseModel, Field, ConfigDict, field_validator, model_validator
from app.core.validation import paise, utc_timestamp
from app.models.policy import TransactionRequest

ID = Annotated[str, Field(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9_.:-]+$")]
Money = Annotated[float, Field(gt=0, le=1_000_000_000, allow_inf_nan=False)]


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class PolicyIn(StrictModel):
    agent_id: ID
    owner_human_id: ID
    authorized_goal: str = Field(
        default="buy weekly groceries under budget", min_length=1, max_length=4000
    )
    max_amount_per_txn: Money = 2000
    max_daily_total: Money = 5000
    allowed_categories: list[Annotated[str, Field(min_length=1, max_length=100)]] = (
        Field(default_factory=lambda: ["grocery"], min_length=1, max_length=100)
    )
    blocked_merchants: list[Annotated[str, Field(min_length=1, max_length=200)]] = (
        Field(default_factory=list, max_length=500)
    )
    allowed_merchants: (
        list[Annotated[str, Field(min_length=1, max_length=200)]] | None
    ) = Field(default=None, max_length=500)
    time_window_start: int = Field(default=6, ge=0, le=23)
    time_window_end: int = Field(default=22, ge=0, le=23)
    velocity_max_count: int = Field(default=3, ge=1, le=100000)
    velocity_window_minutes: int = Field(default=1440, ge=1, le=43200)

    @field_validator("max_amount_per_txn", "max_daily_total")
    @classmethod
    def precise_money(cls, value):
        paise(value)
        return value


class TransactionIn(StrictModel):
    request_id: ID = Field(default_factory=lambda: f"req_{uuid.uuid4().hex}")
    agent_id: ID
    merchant_name: str = Field(min_length=1, max_length=200)
    merchant_category: str = Field(min_length=1, max_length=100)
    amount: Money
    requested_at_hour: int = Field(default=0, ge=0, le=23)
    requested_at_iso: str = Field(default="2000-01-01T00:00:00+00:00", max_length=40)
    stated_intent: str = Field(min_length=1, max_length=4000)
    raw_agent_goal: str = Field(default="", max_length=4000)

    @field_validator("amount")
    @classmethod
    def precise_money(cls, value):
        paise(value)
        return value

    @model_validator(mode="after")
    def valid_time(self):
        ts = utc_timestamp(self.requested_at_iso)
        if ts.hour != self.requested_at_hour:
            raise ValueError("requested_at_hour must match UTC timestamp")
        return self

    def domain(self, policy):
        # A caller cannot choose its own goal, event clock or category authority.
        # Merchant/category remain assertions; use trusted provider IDs before live use.
        data = self.model_dump()
        data["raw_agent_goal"] = policy.authorized_goal
        now = datetime.now(timezone.utc)
        data["requested_at_iso"], data["requested_at_hour"] = now.isoformat(), now.hour
        return TransactionRequest(**data)


class HoldResolutionIn(StrictModel):
    human_verdict: Literal["APPROVE", "BLOCK"]
    reason: str = Field(default="Human review", min_length=1, max_length=2000)


class BatchEvaluateIn(StrictModel):
    requests: list[TransactionIn] = Field(min_length=1, max_length=100)


class SimulateIn(StrictModel):
    agent_id: ID
    goal: str = Field(default="buy weekly groceries under budget", max_length=4000)
    mode: Literal["well_behaved", "adversarial"] = "well_behaved"
    drift_type: Literal[
        "none",
        "amount_creep",
        "category_drift",
        "velocity_spike",
        "off_hours",
        "prompt_injection",
        "merchant_swap",
    ] = "none"
    num_transactions: int = Field(default=5, ge=1, le=10)

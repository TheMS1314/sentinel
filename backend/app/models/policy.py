"""
Sentinel Policy Models
=======================
Defines the schema for an agent spending policy — the "ground truth"
that every transaction request is checked against.

Design principle: policies are pure data (JSON-serializable), never code.
This keeps the rule engine deterministic and auditable — a judge (or an
ops team) can read a policy file and know exactly what an agent can do,
with zero ambiguity.
"""

from __future__ import annotations
from dataclasses import dataclass, field, asdict
from typing import Optional
import json


@dataclass
class TimeWindow:
    """Allowed hours of operation, in 24h local time."""

    start_hour: int = 0  # 0-23
    end_hour: int = 23  # 0-23, inclusive

    def contains(self, hour: int) -> bool:
        if self.start_hour <= self.end_hour:
            return self.start_hour <= hour <= self.end_hour
        # wraps past midnight, e.g. 22 -> 6
        return hour >= self.start_hour or hour <= self.end_hour


@dataclass
class VelocityLimit:
    """Max number of transactions allowed within a rolling window."""

    max_count: int = 3
    window_minutes: int = 1440  # default: per day


@dataclass
class AgentPolicy:
    """
    A complete, self-contained spending policy for one AI agent.
    This is what a human configures once, and Sentinel enforces forever
    (until the human changes it again) — no autonomous self-modification.
    """

    policy_id: str
    agent_id: str
    owner_human_id: str

    # Spend limits
    max_amount_per_txn: float = 2000.0  # in INR
    max_daily_total: float = 5000.0  # in INR

    # Merchant controls
    allowed_categories: list[str] = field(default_factory=lambda: ["grocery"])
    blocked_merchants: list[str] = field(default_factory=list)
    allowed_merchants: Optional[list[str]] = (
        None  # if set, ONLY these are allowed (allowlist mode)
    )

    # Timing controls
    time_window: TimeWindow = field(default_factory=TimeWindow)

    # Velocity controls
    velocity: VelocityLimit = field(default_factory=VelocityLimit)

    # Behavior
    active: bool = True
    version: int = 1
    authorized_goal: str = "buy weekly groceries under budget"

    def to_dict(self) -> dict:
        d = asdict(self)
        return d

    @staticmethod
    def from_dict(d: dict) -> "AgentPolicy":
        d = dict(d)
        if "time_window" in d and isinstance(d["time_window"], dict):
            d["time_window"] = TimeWindow(**d["time_window"])
        if "velocity" in d and isinstance(d["velocity"], dict):
            d["velocity"] = VelocityLimit(**d["velocity"])
        return AgentPolicy(**d)

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), indent=2)


@dataclass
class TransactionRequest:
    """An incoming request from a purchasing agent to spend money."""

    request_id: str
    agent_id: str
    merchant_name: str
    merchant_category: str
    amount: float
    requested_at_hour: int  # 0-23, local time of the request
    requested_at_iso: str  # full ISO timestamp for logging
    stated_intent: str  # what the agent claims it's buying / why
    raw_agent_goal: str = ""  # the original human-given goal, for drift detection


VERDICT_APPROVE = "APPROVE"
VERDICT_HOLD = "HOLD"  # HITL — needs human confirmation
VERDICT_BLOCK = "BLOCK"


@dataclass
class Decision:
    """The output of Sentinel's evaluation of a TransactionRequest."""

    request_id: str
    verdict: str  # APPROVE / HOLD / BLOCK
    reasons: list[str]  # human-readable, specific reasons
    checked_rules: list[str]  # which rules were evaluated (for audit)
    decided_by: str  # "rule_engine" or "llm_reasoner"
    similar_past_cases: list[str] = field(
        default_factory=list
    )  # RAG retrieval, if used
    confidence: Optional[float] = None  # only set by llm_reasoner
    model_version: Optional[str] = (
        None  # exact model string that produced this decision,
    )
    # when decided_by="llm_reasoner" -- lets you tell
    # which model version is behind any given past
    # decision after a model swap, which "just log the
    # provider name" doesn't give you
    risk_score: Optional[float] = None
    policy_id: Optional[str] = None
    policy_version: Optional[int] = None
    prompt_version: Optional[str] = None
    prompt_hash: Optional[str] = None
    llm_mode: Optional[str] = None
    token_usage: Optional[dict] = None


@dataclass
class HistoryItem:
    """A past transaction paired with its actual outcome -- used by the
    rule engine so it can distinguish money that was genuinely spent
    (APPROVE, or a HOLD later resolved to APPROVE by a human) from
    attempts that were blocked or are still pending and never moved
    any money. Summing blocked attempts into 'today's spend' would be
    wrong -- a rejected ₹5000 attempt didn't spend ₹5000."""

    request: TransactionRequest
    verdict: str
    human_override: Optional[str] = None
    payment_status: Optional[str] = None
    budget_day: Optional[str] = None

    @property
    def is_approved_behavior(self) -> bool:
        if self.payment_status is None:
            return (
                self.verdict == VERDICT_APPROVE
                or self.human_override == VERDICT_APPROVE
            )
        return self.payment_status in ("created", "mock", "captured")

    @property
    def counts_against_budget(self) -> bool:
        return self.counts_as_spent or self.payment_status in (
            "reserved",
            "pending",
            "processing",
            "unknown",
            "created",
            "mock",
        )

    @property
    def counts_as_spent(self) -> bool:
        """True only if this transaction actually resulted in money moving."""
        if self.payment_status is not None:
            return self.payment_status == "captured"
        # Compatibility for pure rule-engine fixtures with no payment ledger.
        return self.verdict == VERDICT_APPROVE or self.human_override == VERDICT_APPROVE

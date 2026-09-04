"""
Behavioral Baseline
=====================
A lightweight statistical anomaly layer, separate from the static
policy rules in rule_engine.py. Static rules answer "is this within
the fixed limits a human set?" -- this answers a different question:
"is this normal FOR THIS AGENT, given its own history?"

Why this matters: a transaction can pass every static rule (under the
cap, right category, right hours) and still be a real behavioral
anomaly -- an agent that has spent Rs150-400 per transaction for weeks
suddenly spending Rs1900 (still under a Rs2000 cap) is a genuine
deviation from its own pattern, even though no fixed rule catches it.
This is closer to how real fraud detection works: baseline the normal
range, flag statistical outliers, not just fixed-threshold violations.

Method: simple z-score against the agent's own approved-transaction
history for amount, plus a check for whether the request falls outside
the agent's own typical hour-of-day range. Deliberately NOT a complex
model -- with a modest history size, a heavier model would be both
harder to explain to a human reviewer and more likely to overfit noise.
A z-score is transparent: the exact number that triggered a flag can be
shown to a human, which matters for an auditable trust system.

Scope note: this only uses amount and time-of-day, both already
captured on every transaction. It deliberately does NOT attempt
bank-account/payment-instrument-level profiling -- that would need
data this system doesn't currently capture, and Razorpay's actual
agent-payment APIs may not expose that granularity for agent-initiated
transactions in the first place. Documented as a possible future
extension, not implemented here on unverified assumptions about
available data.
"""

from __future__ import annotations
from dataclasses import dataclass
from statistics import mean, stdev
from typing import Optional

MIN_HISTORY_FOR_BASELINE = 5  # below this, there's not enough signal to baseline meaningfully
AMOUNT_ZSCORE_THRESHOLD = 2.0  # ~95th percentile under a normal approximation
HOUR_OUTLIER_MARGIN = 3  # hours outside the agent's observed [min-margin, max+margin] range


@dataclass
class BehavioralFlag:
    is_anomaly: bool
    reason: str = ""
    amount_zscore: Optional[float] = None
    baseline_mean: Optional[float] = None
    baseline_stdev: Optional[float] = None
    typical_hour_range: Optional[tuple] = None


class BehavioralBaseline:
    """Stateless evaluator -- takes an agent's past APPROVED transactions
    (never blocked/held-then-blocked ones, for the same reason the daily
    cap only counts real spend) and checks whether a new request is a
    statistical outlier against that agent's own pattern."""

    def evaluate(self, history_items: list, request) -> BehavioralFlag:
        approved_amounts = [
            item.request.amount for item in history_items if item.counts_as_spent
        ]
        approved_hours = [
            item.request.requested_at_hour for item in history_items if item.counts_as_spent
        ]

        if len(approved_amounts) < MIN_HISTORY_FOR_BASELINE:
            # Not enough history to baseline meaningfully yet -- a young
            # agent's first few transactions can't be judged as "typical"
            # or "atypical" against a pattern that doesn't exist yet.
            return BehavioralFlag(is_anomaly=False, reason="Insufficient history for a behavioral baseline.")

        baseline_mean = mean(approved_amounts)
        # stdev needs 2+ points and can be 0 if every past amount was
        # identical -- guard both, and treat a zero-variance baseline as
        # "any deviation at all is notable" rather than dividing by zero.
        baseline_stdev = stdev(approved_amounts) if len(approved_amounts) >= 2 else 0.0

        if baseline_stdev > 0:
            zscore = (request.amount - baseline_mean) / baseline_stdev
        else:
            zscore = 0.0 if request.amount == baseline_mean else float("inf")

        typical_hour_range = (min(approved_hours), max(approved_hours))
        hour_is_outlier = (
            request.requested_at_hour < typical_hour_range[0] - HOUR_OUTLIER_MARGIN
            or request.requested_at_hour > typical_hour_range[1] + HOUR_OUTLIER_MARGIN
        )

        amount_is_outlier = abs(zscore) > AMOUNT_ZSCORE_THRESHOLD

        if amount_is_outlier or hour_is_outlier:
            reasons = []
            if amount_is_outlier:
                reasons.append(
                    f"amount ₹{request.amount:.2f} is {abs(zscore):.1f} standard deviations from "
                    f"this agent's typical ₹{baseline_mean:.2f} (based on {len(approved_amounts)} past transactions)"
                )
            if hour_is_outlier:
                reasons.append(
                    f"hour {request.requested_at_hour}:00 is well outside this agent's typical "
                    f"{typical_hour_range[0]}:00-{typical_hour_range[1]}:00 activity window"
                )
            return BehavioralFlag(
                is_anomaly=True,
                reason="Behavioral anomaly: " + "; ".join(reasons) + ".",
                amount_zscore=zscore if zscore != float("inf") else None,
                baseline_mean=baseline_mean,
                baseline_stdev=baseline_stdev,
                typical_hour_range=typical_hour_range,
            )

        return BehavioralFlag(
            is_anomaly=False,
            reason="Within this agent's typical behavioral pattern.",
            amount_zscore=zscore if zscore != float("inf") else None,
            baseline_mean=baseline_mean,
            baseline_stdev=baseline_stdev,
            typical_hour_range=typical_hour_range,
        )

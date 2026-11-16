"""
Composite Risk Scorer
========================
BehavioralBaseline (behavioral_baseline.py) answers one question with a
binary flag: "is this amount or timing a statistical outlier?" That's
useful but limited -- it can't express "nothing here alone is quite
enough to flag, but three mildly-unusual signals together are."

This module combines FOUR weak signals into one weighted 0-100 score,
closer to how production fraud-scoring engines actually work:

  - amount deviation        (0-40 pts)  -- reuses BehavioralBaseline's
                                            z-score computation
  - timing deviation        (0-20 pts)  -- reuses BehavioralBaseline's
                                            typical-hour-range check
  - merchant unfamiliarity  (0-20 pts)  -- has this agent ever
                                            transacted with this
                                            merchant before?
  - category unfamiliarity  (0-20 pts)  -- has this agent ever
                                            transacted in this category
                                            before? (distinct from the
                                            hard allowed_categories
                                            policy check -- this is
                                            about behavioral novelty
                                            WITHIN an already-allowed
                                            category, not a violation)

A request crossing FLAG_THRESHOLD total points gets held for review --
which can happen from one strong signal (e.g. a large amount deviation
alone) or several mild ones stacking up (a slightly unusual amount at
a slightly unusual hour with an unfamiliar merchant), which a single
binary check would miss entirely.
"""

from __future__ import annotations
from dataclasses import dataclass, field
from typing import Optional

from app.core.behavioral_baseline import (
    BehavioralBaseline,
    MIN_HISTORY_FOR_BASELINE,
    circular_hour_distance,
)

FLAG_THRESHOLD = 30

AMOUNT_WEIGHT = 40
TIMING_WEIGHT = 20
MERCHANT_WEIGHT = 20
CATEGORY_WEIGHT = 20


@dataclass
class RiskComponent:
    name: str
    points: float
    max_points: float
    explanation: str


@dataclass
class RiskScore:
    total: float
    max_total: float
    components: list = field(default_factory=list)
    is_high_risk: bool = False
    insufficient_history: bool = False


class RiskScorer:
    def __init__(self, behavioral_baseline: Optional[BehavioralBaseline] = None):
        self.behavioral = behavioral_baseline or BehavioralBaseline()

    def evaluate(self, history_items: list, request) -> RiskScore:
        approved_items = [item for item in history_items if item.is_approved_behavior]

        if len(approved_items) < MIN_HISTORY_FOR_BASELINE:
            return RiskScore(
                total=0,
                max_total=100,
                components=[],
                is_high_risk=False,
                insufficient_history=True,
            )

        flag = self.behavioral.evaluate(history_items, request)
        components = []

        amount_points = 0.0
        if flag.amount_zscore is not None:
            amount_points = min(AMOUNT_WEIGHT, abs(flag.amount_zscore) * 15)
        elif (
            flag.baseline_stdev == 0
            and flag.baseline_mean is not None
            and request.amount != flag.baseline_mean
        ):
            amount_points = AMOUNT_WEIGHT
        components.append(
            RiskComponent(
                name="amount_deviation",
                points=round(amount_points, 1),
                max_points=AMOUNT_WEIGHT,
                explanation=(
                    f"amount is {abs(flag.amount_zscore):.1f}sigma from this agent's typical spend"
                    if flag.amount_zscore is not None
                    else (
                        "amount differs from a zero-variance baseline"
                        if amount_points
                        else "amount matches this agent's typical spend"
                    )
                ),
            )
        )

        timing_points = 0.0
        if flag.typical_hour_range is not None:
            distance = circular_hour_distance(
                [i.request.requested_at_hour for i in approved_items],
                request.requested_at_hour,
            )
            timing_points = min(TIMING_WEIGHT, distance * 4)
        components.append(
            RiskComponent(
                name="timing_deviation",
                points=round(timing_points, 1),
                max_points=TIMING_WEIGHT,
                explanation=(
                    f"hour {request.requested_at_hour}:00 is outside this agent's typical "
                    f"{flag.typical_hour_range[0]}:00-{flag.typical_hour_range[1]}:00 window"
                    if timing_points > 0
                    else "timing matches this agent's typical activity window"
                ),
            )
        )

        known_merchants = {
            item.request.merchant_name.strip().casefold() for item in approved_items
        }
        merchant_points = (
            0.0
            if request.merchant_name.strip().casefold() in known_merchants
            else MERCHANT_WEIGHT
        )
        components.append(
            RiskComponent(
                name="merchant_familiarity",
                points=merchant_points,
                max_points=MERCHANT_WEIGHT,
                explanation=(
                    f"'{request.merchant_name}' is a new merchant for this agent"
                    if merchant_points > 0
                    else f"'{request.merchant_name}' is a familiar merchant for this agent"
                ),
            )
        )

        known_categories = {item.request.merchant_category for item in approved_items}
        category_points = (
            0.0 if request.merchant_category in known_categories else CATEGORY_WEIGHT
        )
        components.append(
            RiskComponent(
                name="category_familiarity",
                points=category_points,
                max_points=CATEGORY_WEIGHT,
                explanation=(
                    f"'{request.merchant_category}' is a new category for this agent"
                    if category_points > 0
                    else f"'{request.merchant_category}' is a familiar category for this agent"
                ),
            )
        )

        total = sum(c.points for c in components)
        return RiskScore(
            total=round(total, 1),
            max_total=100,
            components=components,
            is_high_risk=total >= FLAG_THRESHOLD,
            insufficient_history=False,
        )

    @staticmethod
    def format_reason(score: RiskScore) -> str:
        if score.insufficient_history:
            return "Insufficient history for a composite risk score."
        driving = sorted(
            [c for c in score.components if c.points > 0], key=lambda c: -c.points
        )
        if not driving:
            return f"Composite risk score {score.total:.0f}/100 -- within normal range."
        breakdown = "; ".join(f"{c.name} +{c.points:.0f}" for c in driving)
        return f"Composite risk score {score.total:.0f}/100 ({breakdown})."

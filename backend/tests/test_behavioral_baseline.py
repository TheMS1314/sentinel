"""
Tests for the behavioral baseline anomaly layer.
Run with: pytest backend/tests/test_behavioral_baseline.py -v
"""
import sys
import os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import pytest
from app.models.policy import TransactionRequest, HistoryItem, VERDICT_APPROVE, VERDICT_BLOCK
from app.core.behavioral_baseline import BehavioralBaseline, MIN_HISTORY_FOR_BASELINE


def make_request(**overrides) -> TransactionRequest:
    defaults = dict(
        request_id="req_1",
        agent_id="agent_1",
        merchant_name="FreshMart",
        merchant_category="grocery",
        amount=300.0,
        requested_at_hour=14,
        requested_at_iso="2026-08-24T14:00:00",
        stated_intent="buy groceries",
        raw_agent_goal="buy groceries",
    )
    defaults.update(overrides)
    return TransactionRequest(**defaults)


def make_approved_history(amounts, hours=None):
    hours = hours or [14] * len(amounts)
    return [
        HistoryItem(
            request=make_request(request_id=f"h{i}", amount=a, requested_at_hour=h),
            verdict=VERDICT_APPROVE,
        )
        for i, (a, h) in enumerate(zip(amounts, hours))
    ]


class TestInsufficientHistory:
    def test_below_minimum_history_never_flags(self):
        baseline = BehavioralBaseline()
        history = make_approved_history([300.0, 320.0])  # below MIN_HISTORY_FOR_BASELINE
        result = baseline.evaluate(history, make_request(amount=5000.0))  # would be a huge outlier if scored
        assert result.is_anomaly is False
        assert "insufficient" in result.reason.lower()


class TestAmountOutlier:
    def test_consistent_small_amounts_then_big_spike_flagged(self):
        baseline = BehavioralBaseline()
        history = make_approved_history([200.0, 210.0, 190.0, 205.0, 195.0, 200.0])
        result = baseline.evaluate(history, make_request(amount=1900.0))
        assert result.is_anomaly is True
        assert "standard deviations" in result.reason

    def test_amount_within_normal_range_not_flagged(self):
        baseline = BehavioralBaseline()
        history = make_approved_history([200.0, 250.0, 300.0, 220.0, 280.0, 260.0])
        result = baseline.evaluate(history, make_request(amount=270.0))
        assert result.is_anomaly is False

    def test_zero_variance_baseline_flags_any_deviation(self):
        """If an agent has ALWAYS spent exactly the same amount, any
        different amount is notable even without a computable stdev."""
        baseline = BehavioralBaseline()
        history = make_approved_history([200.0] * 6)
        result = baseline.evaluate(history, make_request(amount=201.0))
        assert result.is_anomaly is True


class TestHourOutlier:
    def test_typical_hours_then_odd_hour_flagged(self):
        baseline = BehavioralBaseline()
        history = make_approved_history(
            [200.0, 210.0, 190.0, 205.0, 195.0, 200.0],
            hours=[9, 10, 11, 9, 10, 11],
        )
        result = baseline.evaluate(history, make_request(amount=200.0, requested_at_hour=23))
        assert result.is_anomaly is True
        assert "typical" in result.reason.lower()

    def test_hour_within_margin_not_flagged(self):
        baseline = BehavioralBaseline()
        history = make_approved_history(
            [200.0, 210.0, 190.0, 205.0, 195.0, 200.0],
            hours=[9, 10, 11, 9, 10, 11],
        )
        # within HOUR_OUTLIER_MARGIN of the observed 9-11 range
        result = baseline.evaluate(history, make_request(amount=200.0, requested_at_hour=13))
        assert result.is_anomaly is False


class TestOnlyCountsApprovedHistory:
    def test_blocked_history_excluded_from_baseline(self):
        """Same principle as the daily-total fix -- a blocked attempt
        never actually happened as real behavior, so it shouldn't shape
        what counts as 'normal' for this agent."""
        baseline = BehavioralBaseline()
        approved = make_approved_history([200.0, 210.0, 190.0, 205.0, 195.0])
        blocked = [
            HistoryItem(request=make_request(request_id="blocked1", amount=9000.0), verdict=VERDICT_BLOCK)
        ]
        history = approved + blocked
        result = baseline.evaluate(history, make_request(amount=205.0))
        assert result.is_anomaly is False
        assert result.baseline_mean < 300  # nowhere near being pulled toward 9000


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))

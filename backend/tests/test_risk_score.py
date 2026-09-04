"""
Tests for the composite risk scorer.
Run with: pytest backend/tests/test_risk_score.py -v
"""
import sys
import os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import pytest
from app.models.policy import TransactionRequest, HistoryItem, VERDICT_APPROVE
from app.core.risk_score import RiskScorer, FLAG_THRESHOLD


def make_request(**overrides) -> TransactionRequest:
    defaults = dict(
        request_id="req_1", agent_id="agent_1", merchant_name="FreshMart",
        merchant_category="grocery", amount=300.0, requested_at_hour=14,
        requested_at_iso="2026-08-24T14:00:00", stated_intent="buy groceries",
        raw_agent_goal="buy groceries",
    )
    defaults.update(overrides)
    return TransactionRequest(**defaults)


def make_approved_history(amounts, hours=None, merchants=None, categories=None):
    n = len(amounts)
    hours = hours or [14] * n
    merchants = merchants or ["FreshMart"] * n
    categories = categories or ["grocery"] * n
    return [
        HistoryItem(
            request=make_request(request_id=f"h{i}", amount=a, requested_at_hour=h,
                                  merchant_name=m, merchant_category=c),
            verdict=VERDICT_APPROVE,
        )
        for i, (a, h, m, c) in enumerate(zip(amounts, hours, merchants, categories))
    ]


class TestInsufficientHistory:
    def test_below_minimum_never_flags(self):
        scorer = RiskScorer()
        history = make_approved_history([300.0, 310.0])
        score = scorer.evaluate(history, make_request(amount=5000.0))
        assert score.insufficient_history is True
        assert score.is_high_risk is False


class TestSingleStrongSignal:
    def test_large_amount_deviation_alone_crosses_threshold(self):
        scorer = RiskScorer()
        history = make_approved_history([200.0, 210.0, 190.0, 205.0, 195.0, 200.0])
        score = scorer.evaluate(history, make_request(amount=1900.0))
        assert score.is_high_risk is True
        assert score.total >= FLAG_THRESHOLD

    def test_everything_normal_stays_low_risk(self):
        scorer = RiskScorer()
        history = make_approved_history([200.0, 250.0, 300.0, 220.0, 280.0, 260.0])
        score = scorer.evaluate(history, make_request(amount=270.0))
        assert score.is_high_risk is False
        assert score.total < FLAG_THRESHOLD


class TestCombinedWeakSignals:
    def test_multiple_mild_signals_combine_to_flag_what_no_single_signal_would(self):
        scorer = RiskScorer()
        history = make_approved_history(
            [200.0, 210.0, 190.0, 205.0, 195.0, 200.0],
            hours=[10, 11, 10, 11, 10, 11],
            merchants=["FreshMart"] * 6,
            categories=["grocery"] * 6,
        )
        mild_request = make_request(
            amount=230.0,
            requested_at_hour=15,
            merchant_name="NewMart",
            merchant_category="grocery",
        )
        score = scorer.evaluate(history, mild_request)
        merchant_component = next(c for c in score.components if c.name == "merchant_familiarity")
        assert merchant_component.points == 20.0
        assert score.total >= FLAG_THRESHOLD
        assert score.is_high_risk is True

    def test_unfamiliar_merchant_alone_does_not_necessarily_flag(self):
        scorer = RiskScorer()
        history = make_approved_history(
            [200.0, 210.0, 190.0, 205.0, 195.0, 200.0],
            hours=[14] * 6,
        )
        request = make_request(amount=200.0, requested_at_hour=14, merchant_name="NewMart")
        score = scorer.evaluate(history, request)
        assert score.total == 20.0
        assert score.is_high_risk is False


class TestFormatReason:
    def test_insufficient_history_message(self):
        scorer = RiskScorer()
        history = make_approved_history([200.0, 210.0])
        score = scorer.evaluate(history, make_request())
        msg = RiskScorer.format_reason(score)
        assert "insufficient" in msg.lower()

    def test_high_risk_message_lists_driving_components(self):
        scorer = RiskScorer()
        history = make_approved_history([200.0] * 6, hours=[14] * 6)
        score = scorer.evaluate(history, make_request(amount=1900.0, merchant_name="NewMart"))
        msg = RiskScorer.format_reason(score)
        assert "amount_deviation" in msg or "merchant_familiarity" in msg
        assert str(int(score.total)) in msg


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))

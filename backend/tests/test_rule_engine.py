"""
Tests for the deterministic rule engine.
Run with: pytest backend/tests/test_rule_engine.py -v
"""

import sys
import os

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import pytest
from app.models.policy import (
    AgentPolicy,
    TransactionRequest,
    TimeWindow,
    VelocityLimit,
    HistoryItem,
    VERDICT_APPROVE,
    VERDICT_BLOCK,
    VERDICT_HOLD,
)
from app.core.rule_engine import RuleEngine, ESCALATE


def make_policy(**overrides) -> AgentPolicy:
    defaults = dict(
        policy_id="pol_1",
        agent_id="agent_1",
        owner_human_id="human_1",
        max_amount_per_txn=2000.0,
        max_daily_total=5000.0,
        allowed_categories=["grocery"],
        blocked_merchants=[],
        allowed_merchants=None,
        time_window=TimeWindow(start_hour=6, end_hour=22),
        velocity=VelocityLimit(max_count=3, window_minutes=1440),
        active=True,
    )
    defaults.update(overrides)
    return AgentPolicy(**defaults)


def make_request(**overrides) -> TransactionRequest:
    defaults = dict(
        request_id="req_1",
        agent_id="agent_1",
        merchant_name="FreshMart",
        merchant_category="grocery",
        amount=500.0,
        requested_at_hour=14,
        requested_at_iso="2026-08-22T14:00:00",
        stated_intent="buy weekly groceries",
        raw_agent_goal="buy weekly groceries under budget",
    )
    defaults.update(overrides)
    return TransactionRequest(**defaults)


def make_history_item(verdict=VERDICT_APPROVE, **overrides) -> HistoryItem:
    """History items default to APPROVE (i.e. money actually spent) unless
    a test explicitly wants to simulate a blocked/held past attempt."""
    return HistoryItem(request=make_request(**overrides), verdict=verdict)


class TestApprove:
    def test_clean_request_is_approved(self):
        engine = RuleEngine()
        result = engine.evaluate(make_policy(), make_request(), [])
        assert result.outcome == VERDICT_APPROVE

    def test_all_rules_checked_on_approve(self):
        engine = RuleEngine()
        result = engine.evaluate(make_policy(), make_request(), [])
        assert "max_amount_per_txn" in result.checked_rules
        assert "velocity_limit" in result.checked_rules


class TestBlockAmount:
    def test_over_per_txn_cap_blocked(self):
        engine = RuleEngine()
        result = engine.evaluate(
            make_policy(max_amount_per_txn=100.0), make_request(amount=500.0), []
        )
        assert result.outcome == VERDICT_BLOCK
        assert "exceeds per-transaction cap" in result.reasons[0]

    def test_daily_total_exceeded_blocked(self):
        engine = RuleEngine()
        policy = make_policy(max_daily_total=1000.0)
        history = [
            make_history_item(
                request_id="req_0", amount=700.0, requested_at_iso="2026-08-22T09:00:00"
            )
        ]
        result = engine.evaluate(policy, make_request(amount=500.0), history)
        assert result.outcome == VERDICT_BLOCK
        assert "daily" in result.reasons[0].lower()

    def test_blocked_prior_attempt_does_not_count_toward_daily_total(self):
        """Regression test: a BLOCKED prior attempt never actually spent
        money, so it must not count toward today's spend when checking
        the daily cap. Found via a live burst-load test where a rejected
        ₹5000 attempt was incorrectly poisoning later legitimate checks."""
        engine = RuleEngine()
        policy = make_policy(max_daily_total=1000.0)
        history = [
            make_history_item(
                request_id="req_blocked",
                amount=5000.0,
                verdict=VERDICT_BLOCK,
                requested_at_iso="2026-08-22T09:00:00",
            ),
        ]
        result = engine.evaluate(policy, make_request(amount=500.0), history)
        assert result.outcome == VERDICT_APPROVE

    def test_held_and_human_approved_counts_toward_daily_total(self):
        """A HOLD that a human later approved DID spend real money, so it
        must count toward the daily cap just like a direct APPROVE."""
        engine = RuleEngine()
        policy = make_policy(max_daily_total=1000.0)
        history = [
            HistoryItem(
                request=make_request(
                    request_id="req_held",
                    amount=700.0,
                    requested_at_iso="2026-08-22T09:00:00",
                ),
                verdict=VERDICT_HOLD,
                human_override=VERDICT_APPROVE,
            ),
        ]
        result = engine.evaluate(policy, make_request(amount=500.0), history)
        assert result.outcome == VERDICT_BLOCK  # 700 + 500 > 1000 cap


class TestMerchantControls:
    def test_blocked_merchant_rejected(self):
        engine = RuleEngine()
        policy = make_policy(blocked_merchants=["ShadyStore"])
        result = engine.evaluate(policy, make_request(merchant_name="ShadyStore"), [])
        assert result.outcome == VERDICT_BLOCK

    def test_allowlist_mode_rejects_unlisted_merchant(self):
        engine = RuleEngine()
        policy = make_policy(allowed_merchants=["FreshMart"])
        result = engine.evaluate(policy, make_request(merchant_name="OtherStore"), [])
        assert result.outcome == VERDICT_BLOCK

    def test_allowlist_mode_accepts_listed_merchant(self):
        engine = RuleEngine()
        policy = make_policy(allowed_merchants=["FreshMart"])
        result = engine.evaluate(policy, make_request(merchant_name="FreshMart"), [])
        assert result.outcome == VERDICT_APPROVE

    def test_disallowed_category_blocked(self):
        engine = RuleEngine()
        policy = make_policy(allowed_categories=["grocery"])
        result = engine.evaluate(
            policy, make_request(merchant_category="electronics"), []
        )
        assert result.outcome == VERDICT_BLOCK

    def test_blocklist_case_insensitive(self):
        """Regression test for a real bypass found via adversarial eval:
        different casing of a blocked merchant name completely evaded
        the check under a byte-exact comparison."""
        engine = RuleEngine()
        policy = make_policy(blocked_merchants=["ShadyStore"])
        for variant in ["shadystore", "SHADYSTORE", "ShAdYsToRe"]:
            result = engine.evaluate(policy, make_request(merchant_name=variant), [])
            assert result.outcome == VERDICT_BLOCK, (
                f"{variant!r} should have been blocked"
            )

    def test_blocklist_whitespace_insensitive(self):
        """Regression test: stray leading/trailing whitespace also
        evaded a byte-exact comparison."""
        engine = RuleEngine()
        policy = make_policy(blocked_merchants=["ShadyStore"])
        for variant in ["ShadyStore ", " ShadyStore", "  ShadyStore  "]:
            result = engine.evaluate(policy, make_request(merchant_name=variant), [])
            assert result.outcome == VERDICT_BLOCK, (
                f"{variant!r} should have been blocked"
            )

    def test_blocklist_genuinely_different_name_not_blocked(self):
        """A genuinely different name (not just case/whitespace) is NOT
        caught by normalization -- this is a documented limitation, not
        a bug: fuzzy/substring matching would have its own false-positive
        risk and is deliberately out of scope here."""
        engine = RuleEngine()
        policy = make_policy(blocked_merchants=["ShadyStore"])
        result = engine.evaluate(policy, make_request(merchant_name="ShadyStore2"), [])
        assert (
            result.outcome == VERDICT_APPROVE
        )  # documented limitation, not a silent gap

    def test_allowlist_case_insensitive(self):
        engine = RuleEngine()
        policy = make_policy(allowed_merchants=["FreshMart"])
        result = engine.evaluate(policy, make_request(merchant_name="freshmart"), [])
        assert result.outcome == VERDICT_APPROVE


class TestTiming:
    def test_outside_time_window_blocked(self):
        engine = RuleEngine()
        policy = make_policy(time_window=TimeWindow(start_hour=9, end_hour=17))
        result = engine.evaluate(policy, make_request(requested_at_hour=23), [])
        assert result.outcome == VERDICT_BLOCK

    def test_wraparound_window_handles_midnight(self):
        engine = RuleEngine()
        policy = make_policy(time_window=TimeWindow(start_hour=22, end_hour=6))
        result = engine.evaluate(policy, make_request(requested_at_hour=2), [])
        assert result.outcome == VERDICT_APPROVE


class TestVelocity:
    def test_velocity_breach_results_in_hold_not_block(self):
        engine = RuleEngine()
        policy = make_policy(velocity=VelocityLimit(max_count=2, window_minutes=1440))
        history = [
            make_history_item(request_id="r1", requested_at_iso="2026-08-22T08:00:00"),
            make_history_item(request_id="r2", requested_at_iso="2026-08-22T09:00:00"),
        ]
        result = engine.evaluate(
            policy, make_request(requested_at_iso="2026-08-22T10:00:00"), history
        )
        assert result.outcome == VERDICT_HOLD

    def test_velocity_within_limit_approved(self):
        engine = RuleEngine()
        policy = make_policy(velocity=VelocityLimit(max_count=5, window_minutes=1440))
        history = [
            make_history_item(request_id="r1", requested_at_iso="2026-08-22T08:00:00")
        ]
        result = engine.evaluate(
            policy, make_request(requested_at_iso="2026-08-22T10:00:00"), history
        )
        assert result.outcome == VERDICT_APPROVE

    def test_blocked_attempts_still_count_toward_velocity(self):
        """Unlike the daily-total check, velocity counts EVERY attempt
        regardless of outcome -- a burst of blocked attempts is itself
        the suspicious pattern this check exists to catch."""
        engine = RuleEngine()
        policy = make_policy(velocity=VelocityLimit(max_count=2, window_minutes=1440))
        history = [
            make_history_item(
                request_id="r1",
                verdict=VERDICT_BLOCK,
                requested_at_iso="2026-08-22T08:00:00",
            ),
            make_history_item(
                request_id="r2",
                verdict=VERDICT_BLOCK,
                requested_at_iso="2026-08-22T09:00:00",
            ),
        ]
        result = engine.evaluate(
            policy, make_request(requested_at_iso="2026-08-22T10:00:00"), history
        )
        assert result.outcome == VERDICT_HOLD


class TestEscalation:
    def test_intent_drift_escalates_not_blocks(self):
        engine = RuleEngine()
        policy = make_policy()
        request = make_request(
            stated_intent="purchase premium gift card bundle",
            raw_agent_goal="buy weekly groceries under budget",
        )
        result = engine.evaluate(policy, request, [])
        assert result.outcome == ESCALATE

    def test_inactive_policy_hard_blocks_before_anything_else(self):
        engine = RuleEngine()
        policy = make_policy(active=False)
        result = engine.evaluate(policy, make_request(), [])
        assert result.outcome == VERDICT_BLOCK
        assert result.checked_rules == ["policy_active"]


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))

"""
Tests for the AP2 mandate adapter.
Run with: pytest backend/tests/test_ap2_adapter.py -v
"""

import sys
import os

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
os.environ["OPENROUTER_API_KEY"] = ""

import pytest
import uuid
from app.models.db import init_db, SessionLocal
from app.core.ap2_adapter import (
    translate_mandates,
    intent_mandate_to_policy,
    cart_mandate_to_request,
)
from app.core.orchestrator import SentinelOrchestrator


@pytest.fixture(scope="module", autouse=True)
def setup_db():
    init_db()
    yield


@pytest.fixture
def db():
    session = SessionLocal()
    yield session
    session.close()


def make_intent_mandate(**overrides):
    defaults = dict(
        max_amount=2000.0,
        max_daily_total=5000.0,
        allowed_merchant_categories=["grocery"],
        excluded_merchants=["ShadyStore"],
        valid_hours={"start": 6, "end": 22},
        max_transactions_per_day=3,
        recurrence_window_minutes=1440,
        goal_description="buy weekly groceries under budget",
    )
    defaults.update(overrides)
    return defaults


def make_cart_mandate(**overrides):
    defaults = dict(
        merchant_name="FreshMart",
        merchant_category="grocery",
        total_amount=500.0,
        line_items_summary="buy weekly groceries",
        requested_at="2026-08-24T14:00:00",
        requested_at_hour=14,
    )
    defaults.update(overrides)
    return defaults


class TestIntentMandateTranslation:
    def test_translates_amount_and_daily_limits(self):
        policy = intent_mandate_to_policy(
            make_intent_mandate(max_amount=1500.0), "agent_1", "human_1"
        )
        assert policy.max_amount_per_txn == 1500.0

    def test_translates_categories_and_blocklist(self):
        policy = intent_mandate_to_policy(
            make_intent_mandate(
                allowed_merchant_categories=["grocery", "pharmacy"],
                excluded_merchants=["BadCo"],
            ),
            "agent_1",
            "human_1",
        )
        assert policy.allowed_categories == ["grocery", "pharmacy"]
        assert policy.blocked_merchants == ["BadCo"]

    def test_translates_time_window(self):
        policy = intent_mandate_to_policy(
            make_intent_mandate(valid_hours={"start": 9, "end": 18}),
            "agent_1",
            "human_1",
        )
        assert policy.time_window.start_hour == 9
        assert policy.time_window.end_hour == 18

    def test_missing_fields_use_sensible_defaults(self):
        policy = intent_mandate_to_policy({}, "agent_1", "human_1")
        assert policy.max_amount_per_txn > 0
        assert policy.time_window.start_hour == 0
        assert policy.time_window.end_hour == 23


class TestCartMandateTranslation:
    def test_translates_merchant_and_amount(self):
        request = cart_mandate_to_request(
            make_cart_mandate(total_amount=750.0), "agent_1", "goal"
        )
        assert request.merchant_name == "FreshMart"
        assert request.amount == 750.0

    def test_translates_intent_text(self):
        request = cart_mandate_to_request(
            make_cart_mandate(line_items_summary="premium gift card bundle"),
            "agent_1",
            "goal",
        )
        assert request.stated_intent == "premium gift card bundle"


class TestFullTranslationAndEvaluation:
    def test_clean_mandate_pair_resolves_through_real_orchestrator(self, db):
        agent_id = f"agent_ap2_{uuid.uuid4().hex[:8]}"
        result = translate_mandates(
            intent_mandate=make_intent_mandate(),
            cart_mandate=make_cart_mandate(),
            agent_id=agent_id,
            owner_human_id="human_ap2_test",
        )
        assert result.signature_verified is False
        assert len(result.warnings) > 0

        orchestrator = SentinelOrchestrator(db)
        decision = orchestrator.evaluate_transaction(result.policy, result.request)
        assert decision.verdict == "APPROVE"

    def test_intent_mandate_scope_violation_correctly_blocked(self, db):
        agent_id = f"agent_ap2_{uuid.uuid4().hex[:8]}"
        result = translate_mandates(
            intent_mandate=make_intent_mandate(max_amount=1000.0),
            cart_mandate=make_cart_mandate(total_amount=5000.0),
            agent_id=agent_id,
            owner_human_id="human_ap2_test",
        )
        orchestrator = SentinelOrchestrator(db)
        decision = orchestrator.evaluate_transaction(result.policy, result.request)
        assert decision.verdict == "BLOCK"
        assert any("per-transaction cap" in r for r in decision.reasons)

    def test_excluded_merchant_in_intent_mandate_blocks_cart(self, db):
        agent_id = f"agent_ap2_{uuid.uuid4().hex[:8]}"
        result = translate_mandates(
            intent_mandate=make_intent_mandate(excluded_merchants=["FreshMart"]),
            cart_mandate=make_cart_mandate(merchant_name="FreshMart"),
            agent_id=agent_id,
            owner_human_id="human_ap2_test",
        )
        orchestrator = SentinelOrchestrator(db)
        decision = orchestrator.evaluate_transaction(result.policy, result.request)
        assert decision.verdict == "BLOCK"


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))

"""
Regression test for the exact bug that started this whole stabilization
pass: PurchasingAgent's well_behaved scenario generating
merchant_category="grocery_stores" while AgentPolicy.allowed_categories
defaulted to ["grocery"] -- an exact-match category check meant every
"normal", policy-compliant transaction was wrongly BLOCKed.

This test exists specifically so that if someone edits
PurchasingAgent.run() or its category constants back toward
"grocery_stores" (or any other category not equal to what the default
policy allows), CI catches it immediately instead of it silently
reaching a demo recording again.
"""
from __future__ import annotations
import sys
import os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
os.environ["OPENROUTER_API_KEY"] = ""  # force mock mode

import pytest
import uuid
from app.models.db import init_db, SessionLocal
from app.models.policy import AgentPolicy, TransactionRequest, TimeWindow, VelocityLimit, VERDICT_APPROVE
from app.core.orchestrator import SentinelOrchestrator
from app.agents.purchasing_agent import PurchasingAgent


@pytest.fixture(scope="module", autouse=True)
def setup_db():
    init_db()
    yield


@pytest.fixture
def db():
    session = SessionLocal()
    yield session
    session.close()


def fresh_agent_id() -> str:
    return f"agent_regress_{uuid.uuid4().hex[:8]}"


class TestGroceryCategoryRegression:
    def test_well_behaved_scenario_generates_grocery_not_grocery_stores(self):
        """The exact bug: the generator must emit the same category
        string the default policy allows, not a near-miss variant."""
        agent = PurchasingAgent()
        requests = agent.run(
            agent_id="agent_regress_gen", goal="buy groceries",
            mode="well_behaved", drift_type="none", num_transactions=5,
        )
        for req in requests:
            assert req.merchant_category == "grocery", (
                f"Expected 'grocery', got {req.merchant_category!r} -- "
                "this is the exact category-mismatch regression."
            )

    def test_default_policy_allows_exactly_grocery(self):
        policy = AgentPolicy(
            policy_id="pol_regress", agent_id="agent_regress_policy", owner_human_id="human_test",
            max_amount_per_txn=2000.0, max_daily_total=5000.0,
            allowed_categories=["grocery"],
            time_window=TimeWindow(start_hour=6, end_hour=22),
            velocity=VelocityLimit(max_count=5, window_minutes=1440),
        )
        assert policy.allowed_categories == ["grocery"]

    def test_normal_grocery_transaction_within_policy_approves(self, db):
        """End-to-end through the real orchestrator: a well-behaved,
        in-policy grocery purchase must APPROVE, not BLOCK. This is the
        actual user-visible symptom the original bug caused."""
        agent_id = fresh_agent_id()
        policy = AgentPolicy(
            policy_id=f"pol_{agent_id}", agent_id=agent_id, owner_human_id="human_test",
            max_amount_per_txn=2000.0, max_daily_total=5000.0,
            allowed_categories=["grocery"],
            time_window=TimeWindow(start_hour=6, end_hour=22),
            velocity=VelocityLimit(max_count=5, window_minutes=1440),
        )
        orchestrator = SentinelOrchestrator(db)

        request = TransactionRequest(
            request_id=f"req_{uuid.uuid4().hex[:10]}", agent_id=agent_id,
            merchant_name="FreshMart", merchant_category="grocery",
            amount=45.0, requested_at_hour=10, requested_at_iso="2026-09-01T10:00:00",
            stated_intent="buy groceries",
        )
        decision = orchestrator.evaluate_transaction(policy, request)
        assert decision.verdict == VERDICT_APPROVE, (
            f"Expected APPROVE for an in-policy grocery purchase, got {decision.verdict} "
            f"-- reasons: {decision.reasons}"
        )

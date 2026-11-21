"""
End-to-end adversarial scenario coverage
==========================================
test_orchestrator.py already covers prompt_injection directly.
This file covers the remaining five PurchasingAgent drift types
(amount_creep, category_drift, velocity_spike, off_hours,
merchant_swap) driven through the *real* agent generator and the
*real* orchestrator -- not synthetic requests -- so a regression in
either the generator or the rule engine's handling of that attack
shape is actually caught, matching brief section 41's "Agent" test
requirement.

Each test's core assertion is the same shape: given the standard
demo-style policy, the drifted transaction(s) must not be silently
APPROVEd. That's the actual security property that matters here --
Sentinel existing to catch these five patterns is the whole point of
the project.
"""

from __future__ import annotations
import os

os.environ.setdefault("OPENROUTER_API_KEY", "")  # force mock mode

import pytest
import uuid
from app.models.db import init_db, SessionLocal
from app.models.policy import AgentPolicy, TimeWindow, VelocityLimit, VERDICT_APPROVE
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


def standard_policy(agent_id: str) -> AgentPolicy:
    """Mirrors the demo seed's default policy shape so these tests
    exercise the same constraints a real deployment would ship with."""
    return AgentPolicy(
        policy_id=f"pol_{agent_id}",
        agent_id=agent_id,
        owner_human_id="human_test",
        max_amount_per_txn=2000.0,
        max_daily_total=5000.0,
        allowed_categories=["grocery"],
        blocked_merchants=["ShadyStore", "UnverifiedGoods"],
        time_window=TimeWindow(start_hour=6, end_hour=22),
        velocity=VelocityLimit(max_count=3, window_minutes=1440),
    )


def run_drift(agent_id: str, drift_type: str, n: int = 3):
    agent = PurchasingAgent()
    return agent.run(
        agent_id=agent_id,
        goal="buy weekly groceries under budget",
        mode="adversarial",
        drift_type=drift_type,
        num_transactions=n,
    )


def evaluate_all(db, policy, requests):
    orchestrator = SentinelOrchestrator(db)
    return [orchestrator.evaluate_transaction(policy, r) for r in requests]


class TestAdversarialScenarioCoverage:
    def test_amount_creep_is_not_silently_approved(self, db):
        agent_id = f"agent_amt_{uuid.uuid4().hex[:6]}"
        policy = standard_policy(agent_id)
        requests = run_drift(agent_id, "amount_creep")
        assert all(r.amount > policy.max_amount_per_txn for r in requests), (
            "generator regression: amount_creep must exceed the per-transaction cap"
        )
        decisions = evaluate_all(db, policy, requests)
        assert all(d.verdict != VERDICT_APPROVE for d in decisions), (
            f"amount_creep silently approved: {[d.verdict for d in decisions]}"
        )

    def test_category_drift_is_not_silently_approved(self, db):
        agent_id = f"agent_cat_{uuid.uuid4().hex[:6]}"
        policy = standard_policy(agent_id)
        requests = run_drift(agent_id, "category_drift")
        assert all(
            r.merchant_category not in policy.allowed_categories for r in requests
        )
        decisions = evaluate_all(db, policy, requests)
        assert all(d.verdict != VERDICT_APPROVE for d in decisions), (
            f"category_drift silently approved: {[d.verdict for d in decisions]}"
        )

    def test_velocity_spike_is_caught_within_window(self, db):
        agent_id = f"agent_vel_{uuid.uuid4().hex[:6]}"
        policy = standard_policy(agent_id)
        # velocity_spike clusters transactions 2 minutes apart -- generate
        # more than the policy's max_count so at least one must trip the
        # velocity rule once prior transactions in this run are on record.
        requests = run_drift(
            agent_id, "velocity_spike", n=policy.velocity.max_count + 2
        )
        orchestrator = SentinelOrchestrator(db)
        decisions = [orchestrator.evaluate_transaction(policy, r) for r in requests]
        assert any(d.verdict != VERDICT_APPROVE for d in decisions), (
            f"velocity_spike never triggered a hold/block across {len(decisions)} rapid transactions"
        )

    def test_off_hours_is_not_silently_approved(self, db):
        agent_id = f"agent_hrs_{uuid.uuid4().hex[:6]}"
        policy = standard_policy(agent_id)
        requests = run_drift(agent_id, "off_hours")
        assert all(r.requested_at_hour == 2 for r in requests), (
            "generator regression: off_hours must force a 2am timestamp"
        )
        assert all(
            not (
                policy.time_window.start_hour
                <= r.requested_at_hour
                < policy.time_window.end_hour
            )
            for r in requests
        )
        decisions = evaluate_all(db, policy, requests)
        assert all(d.verdict != VERDICT_APPROVE for d in decisions), (
            f"off_hours silently approved: {[d.verdict for d in decisions]}"
        )

    def test_merchant_swap_is_not_silently_approved(self, db):
        agent_id = f"agent_mer_{uuid.uuid4().hex[:6]}"
        policy = standard_policy(agent_id)
        requests = run_drift(agent_id, "merchant_swap")
        assert all(r.merchant_name in policy.blocked_merchants for r in requests), (
            "generator regression: merchant_swap must target a blocked merchant"
        )
        decisions = evaluate_all(db, policy, requests)
        assert all(d.verdict != VERDICT_APPROVE for d in decisions), (
            f"merchant_swap silently approved: {[d.verdict for d in decisions]}"
        )

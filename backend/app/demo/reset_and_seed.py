"""
Demo Reset & Seed Script
==========================
Wipes all Sentinel data and replays a clean, deterministic set of
scenarios -- both the well-behaved happy path and every adversarial
drift type -- so the dashboard has a consistent, presentable state
every time you record or re-record the pitch video.

Usage:
    python -m app.demo.reset_and_seed
"""

from __future__ import annotations
import os
import sys

from app.core.env import load_env
load_env()  # must run before any app import below reads os.environ

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

from app.models.db import init_db, SessionLocal, engine, Base
from app.models.policy import AgentPolicy, TimeWindow, VelocityLimit
from app.core.orchestrator import SentinelOrchestrator
from app.agents.purchasing_agent import PurchasingAgent
from app.models.db import PolicyRecord, TransactionRecord, DecisionRecord

DEMO_AGENT_ID = "agent_demo_1"
DEMO_GOAL = "buy weekly groceries under budget"


def print_db_diagnostic():
    """Which database is actually active -- printed with credentials
    redacted, so 'which DB did the seed write to?' never has to be
    guessed at when the dashboard looks stale or wrong."""
    dialect_name = {"postgresql": "PostgreSQL", "sqlite": "SQLite"}.get(engine.dialect.name, engine.dialect.name)
    db_name = engine.url.database or "?"
    host = engine.url.host
    location = f"{host}/{db_name}" if host else db_name
    queue_backend = "redis" if os.environ.get("SENTINEL_REDIS_URL") else "in-process"
    print(f"Database: {dialect_name} / {location}")
    print(f"Environment: {os.environ.get('SENTINEL_ENV', 'development')}")
    print(f"Queue: {queue_backend}")


def wipe_and_recreate():
    print("Wiping existing Sentinel data...")
    Base.metadata.drop_all(bind=engine)
    Base.metadata.create_all(bind=engine)
    print("Fresh schema created.")


def seed_policy(db) -> AgentPolicy:
    policy = AgentPolicy(
        policy_id="pol_demo_seed",
        agent_id=DEMO_AGENT_ID,
        owner_human_id="human_demo",
        max_amount_per_txn=2000.0,
        max_daily_total=5000.0,
        allowed_categories=["grocery"],
        blocked_merchants=["ShadyStore", "UnverifiedGoods"],
        time_window=TimeWindow(6, 22),
        velocity=VelocityLimit(3, 1440),
    )
    record = PolicyRecord(
        policy_id=policy.policy_id,
        agent_id=policy.agent_id,
        owner_human_id=policy.owner_human_id,
        version=1,
        active=True,
        policy_json=policy.to_dict(),
    )
    db.add(record)
    db.commit()
    print(f"Seeded policy for {DEMO_AGENT_ID}: max Rs2000/txn, Rs5000/day, grocery only, 6am-10pm.")
    return policy


def run_demo_scenarios(db, policy_template: AgentPolicy):
    """Each scenario gets its own agent_id so every guardrail is
    demonstrated cleanly and independently -- a judge watching the pitch
    video shouldn't have to mentally untangle carryover state from a
    previous scenario to understand why a given attack was caught.
    The well-behaved scenario stays on the flagship agent_id and is run
    first, since its own natural accumulation (3 clean approvals, then
    a HOLD on the 4th for velocity) is itself a good demo beat."""
    agent = PurchasingAgent()
    orchestrator = SentinelOrchestrator(db)

    def seed_policy_for(agent_id: str) -> AgentPolicy:
        p = AgentPolicy(
            policy_id=f"pol_{agent_id}",
            agent_id=agent_id,
            owner_human_id="human_demo",
            max_amount_per_txn=policy_template.max_amount_per_txn,
            max_daily_total=policy_template.max_daily_total,
            allowed_categories=policy_template.allowed_categories,
            blocked_merchants=policy_template.blocked_merchants,
            time_window=policy_template.time_window,
            velocity=policy_template.velocity,
        )
        record = PolicyRecord(
            policy_id=p.policy_id, agent_id=agent_id, owner_human_id="human_demo",
            version=1, active=True, policy_json=p.to_dict(),
        )
        db.add(record)
        db.commit()
        return p

    scenarios = [
        (DEMO_AGENT_ID, "well_behaved", "none", 4, "Scenario A: legitimate agent shopping within policy"),
        ("agent_demo_amount_creep", "adversarial", "amount_creep", 2, "Scenario B1: amount creep attack"),
        ("agent_demo_category_drift", "adversarial", "category_drift", 2, "Scenario B2: category drift attack"),
        ("agent_demo_velocity_spike", "adversarial", "velocity_spike", 4, "Scenario B3: velocity spike attack"),
        ("agent_demo_off_hours", "adversarial", "off_hours", 2, "Scenario B4: off-hours attempt"),
        ("agent_demo_prompt_injection", "adversarial", "prompt_injection", 2, "Scenario B5: prompt injection attack"),
        ("agent_demo_merchant_swap", "adversarial", "merchant_swap", 2, "Scenario B6: blocked merchant attempt"),
    ]

    for agent_id, mode, drift, n, label in scenarios:
        print(f"\n{label}")
        print("-" * len(label))
        policy = policy_template if agent_id == DEMO_AGENT_ID else seed_policy_for(agent_id)
        requests = agent.run(agent_id=agent_id, goal=DEMO_GOAL, mode=mode, drift_type=drift, num_transactions=n)
        scenario_tag = mode if mode == "well_behaved" else f"adversarial:{drift}"
        for req in requests:
            decision = orchestrator.evaluate_transaction(policy, req, scenario_tag=scenario_tag)
            print(f"  {req.merchant_name:16} Rs{req.amount:8.2f}  ->  {decision.verdict:8}  ({decision.decided_by})")


def validate_seed(db) -> list[str]:
    """Checks the seed's own output before declaring success. Returns
    a list of problems (empty = valid). This exists specifically so a
    broken seed fails loudly at seed time instead of silently reaching
    a demo recording or a "why is the dashboard empty" debugging
    session."""
    problems = []

    policy = db.query(PolicyRecord).filter_by(agent_id=DEMO_AGENT_ID, active=True).first()
    if policy is None:
        problems.append(f"No active policy found for {DEMO_AGENT_ID}")
    elif policy.policy_json.get("allowed_categories") != ["grocery"]:
        problems.append(f"Expected allowed_categories=['grocery'], got {policy.policy_json.get('allowed_categories')}")

    tx_count = db.query(TransactionRecord).count()
    if tx_count == 0:
        problems.append("Transaction count is 0 -- seed produced no transactions")

    expected_scenarios = {
        "well_behaved", "adversarial:amount_creep", "adversarial:category_drift",
        "adversarial:velocity_spike", "adversarial:off_hours",
        "adversarial:prompt_injection", "adversarial:merchant_swap",
    }
    seen_scenarios = {row[0] for row in db.query(TransactionRecord.scenario_tag).distinct().all()}
    missing = expected_scenarios - seen_scenarios
    if missing:
        problems.append(f"Missing expected scenarios: {sorted(missing)}")

    normal_grocery = (
        db.query(TransactionRecord)
        .filter(TransactionRecord.agent_id == DEMO_AGENT_ID, TransactionRecord.scenario_tag == "well_behaved")
        .all()
    )
    bad_categories = {t.merchant_category for t in normal_grocery} - {"grocery"}
    if bad_categories:
        problems.append(f"Well-behaved scenario used non-grocery categories: {bad_categories} (the exact original regression)")

    drift_category_map = {"category_drift"}
    drift_rows = (
        db.query(TransactionRecord)
        .filter(TransactionRecord.agent_id == "agent_demo_category_drift")
        .all()
    )
    if drift_rows and not any(t.merchant_category != "grocery" for t in drift_rows):
        problems.append("category_drift scenario did not actually drift outside the allowed category")

    return problems


if __name__ == "__main__":
    wipe_and_recreate()
    print_db_diagnostic()
    db = SessionLocal()
    policy = seed_policy(db)
    run_demo_scenarios(db, policy)
    db.commit()

    problems = validate_seed(db)
    tx_total = db.query(TransactionRecord).count()
    policy_total = db.query(PolicyRecord).count()

    if problems:
        print("\nSeed validation FAILED:")
        for p in problems:
            print(f"  - {p}")
        db.close()
        sys.exit(1)

    print(f"\nDemo reset complete.")
    print(f"Transactions: {tx_total}")
    print(f"Policies: {policy_total}")
    print(f"Database: {engine.url.database}")
    print("Seed validation: OK (policy present, categories consistent, all scenarios represented)")
    db.close()

"""Runnable HTTP example for the developer quickstart."""

import json
import uuid
from fastapi.testclient import TestClient
from app.api.main import app
from app.models.db import Base, engine


def test_payment_quickstart(monkeypatch):
    Base.metadata.create_all(engine)
    agent = f"quickstart_{uuid.uuid4().hex}"
    owner = f"owner_{agent}"
    keys = {
        role: f"quickstart-test-{role}-{uuid.uuid4().hex}"
        for role in ("owner", "reviewer", "agent", "other")
    }
    monkeypatch.setenv(
        "SENTINEL_CREDENTIALS",
        json.dumps(
            [
                dict(key=keys["owner"], subject=owner, role="owner", owner_id=owner),
                dict(
                    key=keys["reviewer"],
                    subject="reviewer",
                    role="reviewer",
                    owner_id=owner,
                ),
                dict(key=keys["agent"], subject=agent, role="agent", agent_ids=[agent]),
                dict(
                    key=keys["other"],
                    subject="other",
                    role="agent",
                    agent_ids=["another_agent"],
                ),
            ]
        ),
    )
    monkeypatch.setenv("SENTINEL_DISPATCH_PAUSED", "0")
    client = TestClient(app)

    def post(path, payload, role):
        return client.post(path, json=payload, headers={"X-API-Key": keys[role]})

    policy = post(
        "/policies",
        dict(
            agent_id=agent,
            owner_human_id=owner,
            authorized_goal="buy weekly groceries",
            max_amount_per_txn=60,
            max_daily_total=100,
            allowed_categories=["grocery"],
            allowed_merchants=["Corner Market", "Example Shop"],
            blocked_merchants=["Example Shop"],
            time_window_start=0,
            time_window_end=23,
            velocity_max_count=100,
        ),
        "owner",
    )
    assert policy.status_code == 200
    policy_id = policy.json()["policy_id"]
    budget_path = f"/policies/{agent}/budget"
    agent_headers = {"X-API-Key": keys["agent"]}
    assert (
        client.get(budget_path, headers={"X-API-Key": keys["other"]}).status_code == 403
    )

    def budget(committed):
        result = client.get(budget_path, headers=agent_headers)
        assert result.status_code == 200
        body = result.json()
        assert body["policy_version"] == 1
        assert body["daily_cap_paise"] == 10000
        assert body["committed_paise"] == body["reserved_paise"] == committed
        assert body["captured_paise"] == 0
        assert body["available_paise"] == 10000 - committed

    def purchase(label, amount, **values):
        return (
            dict(
                request_id=f"{agent}_{label}",
                agent_id=agent,
                amount=amount,
                merchant_name="Corner Market",
                merchant_category="grocery",
                stated_intent="buy weekly groceries",
            )
            | values
        )

    def evaluate(request, verdict, rule=None):
        result = post("/transactions/evaluate", request, "agent")
        assert result.status_code == 200
        body = result.json()
        assert body["verdict"] == verdict
        assert body["policy_id"] == policy_id
        if rule:
            assert rule in body["checked_rules"]
        return body

    budget(0)
    allowed = purchase("allow", 40, merchant_name=" corner   market ")
    original = evaluate(allowed, "APPROVE")
    budget(4000)
    assert evaluate(allowed, "APPROVE") == original
    budget(4000)

    blocked = [
        (purchase("blocked", 60, merchant_name="Example Shop"), "merchant_blocklist"),
        (
            purchase("format", 60, merchant_name="  ＥＸＡＭＰＬＥ\t  SHOP "),
            "merchant_blocklist",
        ),
        (
            purchase("punctuation", 60, merchant_name="Example-Shop"),
            "merchant_allowlist",
        ),
    ]
    merchant = post(
        "/merchants",
        dict(
            owner_id=owner,
            legal_name="Example Shop",
            category="grocery",
            domain="example-shop.example",
            operator_reference="Quickstart fixture",
            expected_version=0,
        ),
        "owner",
    )
    assert merchant.status_code == 200
    blocked.append(
        (
            purchase("identity", 60, merchant_id=merchant.json()["merchant_id"]),
            "merchant_blocklist",
        )
    )
    for rejected, rule in blocked:
        evaluate(rejected, "BLOCK", rule)
        budget(4000)

    held = purchase("review", 60, stated_intent="restock pantry supplies")
    evaluate(held, "HOLD")
    budget(4000)
    review_path = f"/holds/{held['request_id']}/resolve"
    review = dict(human_verdict="APPROVE", reason="Verified grocery request")
    assert post(review_path, review, "agent").status_code == 403
    accepted = post(review_path, review, "reviewer")
    assert accepted.status_code == 200 and accepted.json()["status"] == "mock"
    assert post(review_path, review, "reviewer").status_code == 409
    budget(10000)
    evaluate(purchase("over_budget", 1), "BLOCK", "max_daily_total")

    feed = client.get(
        "/transactions", params={"agent_id": agent, "limit": 20}, headers=agent_headers
    )
    assert feed.status_code == 200
    rows = {row["request_id"]: row for row in feed.json()}
    assert rows[allowed["request_id"]]["razorpay_status"] == "mock"
    assert rows[held["request_id"]]["verdict"] == "HOLD"
    assert rows[held["request_id"]]["human_override"] == "APPROVE"
    for rejected, _ in blocked:
        assert rows[rejected["request_id"]]["verdict"] == "BLOCK"

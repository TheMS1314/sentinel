"""Regression tests for authority, concurrency, money and recovery boundaries."""

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
import hashlib
import hmac
import json
import uuid
from unittest.mock import MagicMock
import pytest
from fastapi.testclient import TestClient
from app.models.db import (
    Base,
    engine,
    SessionLocal,
    PolicyRecord,
    TransactionRecord,
    DecisionRecord,
    PaymentRecord,
    ReviewEvent,
)
from app.models.policy import AgentPolicy, TransactionRequest, TimeWindow, VelocityLimit
from app.core.orchestrator import SentinelOrchestrator, ConflictError
from app.core.validation import paise
from app.core.schemas import TransactionIn
from app.core.behavioral_baseline import circular_hour_distance
from app.api.main import app


def uid():
    return uuid.uuid4().hex


@pytest.fixture(autouse=True)
def schema():
    Base.metadata.create_all(engine)


@pytest.fixture
def db():
    db = SessionLocal()
    yield db
    db.close()


def policy(db, **changes):
    aid = "hard_" + uid()
    p = AgentPolicy(
        "pol_" + aid,
        aid,
        "owner_" + aid,
        time_window=TimeWindow(0, 23),
        velocity=VelocityLimit(100, 1440),
        **changes,
    )
    db.add(
        PolicyRecord(
            policy_id=p.policy_id,
            agent_id=aid,
            owner_human_id=p.owner_human_id,
            version=1,
            active=True,
            policy_json=p.to_dict(),
        )
    )
    db.commit()
    return p


def request(p, **changes):
    now = datetime.now(timezone.utc)
    data = dict(
        request_id="req_" + uid(),
        agent_id=p.agent_id,
        merchant_name="FreshMart",
        merchant_category="grocery",
        amount=500,
        requested_at_hour=now.hour,
        requested_at_iso=now.isoformat(),
        stated_intent="buy weekly groceries",
        raw_agent_goal=p.authorized_goal,
    )
    data.update(changes)
    return TransactionRequest(**data)


@pytest.mark.parametrize(
    "amount", [0, -1, float("nan"), float("inf"), "1.001", 1_000_000_001]
)
def test_invalid_money(amount):
    with pytest.raises(ValueError):
        paise(amount)


def test_exact_money():
    assert paise("0.29") == 29
    assert paise("2000.01") == 200001


def test_inconsistent_timestamp_rejected():
    with pytest.raises(ValueError):
        TransactionIn(
            agent_id="a",
            merchant_name="m",
            merchant_category="grocery",
            amount=1,
            stated_intent="x",
            requested_at_hour=2,
            requested_at_iso="2026-01-01T01:00:00Z",
        )


def test_client_goal_and_clock_do_not_authorize(db):
    p = policy(db)
    incoming = TransactionIn(
        agent_id=p.agent_id,
        merchant_name="FreshMart",
        merchant_category="grocery",
        amount=10,
        stated_intent="buy groceries",
        raw_agent_goal="buy anything",
    )
    req = incoming.domain(p)
    assert req.raw_agent_goal == p.authorized_goal
    assert (
        abs(
            (
                datetime.now(timezone.utc)
                - datetime.fromisoformat(req.requested_at_iso)
            ).total_seconds()
        )
        < 2
    )


def test_replay_does_not_create_second_order(db):
    p = policy(db)
    req = request(p)
    o = SentinelOrchestrator(db)
    o.gateway.create_order = MagicMock(
        return_value={"status": "mock", "order_id": "mock_" + uid()}
    )
    first = o.evaluate_transaction(p, req)
    second = o.evaluate_transaction(p, req)
    assert first.verdict == second.verdict == "APPROVE"
    assert o.gateway.create_order.call_count == 1
    assert db.query(TransactionRecord).filter_by(request_id=req.request_id).count() == 1
    assert db.get(PaymentRecord, req.request_id).state == "mock"
    req.amount = 999
    with pytest.raises(ConflictError):
        o.evaluate_transaction(p, req)


def test_unknown_gateway_reserves_budget_and_never_retries(db):
    p = policy(db, max_daily_total=700)
    o = SentinelOrchestrator(db)
    o.gateway.create_order = MagicMock(side_effect=TimeoutError())
    first = request(p, amount=500)
    assert o.evaluate_transaction(p, first).verdict == "APPROVE"
    assert o.dispatch_payment(first.request_id)["status"] == "unknown"
    assert o.evaluate_transaction(p, request(p, amount=300)).verdict == "BLOCK"
    assert o.gateway.create_order.call_count == 1
    item = next(
        x
        for x in o.audit.get_recent_transactions_for_agent(p.agent_id)
        if x.request.request_id == first.request_id
    )
    assert item.counts_against_budget and not item.counts_as_spent


def test_definite_failure_releases_budget(db):
    p = policy(db, max_daily_total=700)
    o = SentinelOrchestrator(db)
    o.gateway.create_order = MagicMock(return_value={"status": "failed"})
    assert o.evaluate_transaction(p, request(p, amount=500)).verdict == "APPROVE"
    assert o.evaluate_transaction(p, request(p, amount=500)).verdict == "APPROVE"


def test_block_cannot_be_human_approved(db):
    p = policy(db)
    r = request(p, amount=9999)
    o = SentinelOrchestrator(db)
    assert o.evaluate_transaction(p, r).verdict == "BLOCK"
    with pytest.raises(ConflictError):
        o.resolve_hold(r.request_id, "APPROVE")
    assert db.get(PaymentRecord, r.request_id) is None


def test_review_once_and_preserves_original(db):
    p = policy(db)
    r = request(p, stated_intent="unclear purpose")
    o = SentinelOrchestrator(db)
    assert o.evaluate_transaction(p, r).verdict == "HOLD"
    assert (
        o.resolve_hold(
            r.request_id,
            "APPROVE",
            reviewer_id="reviewer",
            reason="Confirmed groceries",
        )["status"]
        == "mock"
    )
    with pytest.raises(ConflictError):
        o.resolve_hold(r.request_id, "APPROVE")
    record = db.query(DecisionRecord).filter_by(request_id=r.request_id).one()
    assert record.verdict == "HOLD" and record.human_override == "APPROVE"
    assert record.resolved_by == "reviewer"
    assert db.query(ReviewEvent).filter_by(request_id=r.request_id).count() == 1


def test_review_rechecks_remaining_budget(db):
    p = policy(db, max_daily_total=700)
    o = SentinelOrchestrator(db)
    held = request(p, stated_intent="unclear purpose", amount=500)
    assert o.evaluate_transaction(p, held).verdict == "HOLD"
    assert o.evaluate_transaction(p, request(p, amount=400)).verdict == "APPROVE"
    with pytest.raises(ConflictError):
        o.resolve_hold(held.request_id, "APPROVE")


def test_parallel_spending_never_exceeds_allowance(db):
    p = policy(db, max_daily_total=1000)
    requests = [request(p, amount=700) for _ in range(2)]

    def evaluate(r):
        local = SessionLocal()
        try:
            return SentinelOrchestrator(local).evaluate_transaction(p, r).verdict
        finally:
            local.close()

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(evaluate, requests))
    assert sorted(results) == ["APPROVE", "BLOCK"]


def test_policy_and_prompt_provenance(db):
    p = policy(db)
    r = request(p, stated_intent="groceries for usual needs")
    d = SentinelOrchestrator(db).evaluate_transaction(p, r)
    record = db.query(DecisionRecord).filter_by(request_id=r.request_id).one()
    assert d.policy_id == p.policy_id and d.policy_version == 1
    assert record.policy_snapshot["authorized_goal"] == p.authorized_goal
    assert len(record.prompt_hash) == 64 and record.prompt_version
    assert record.llm_mode == "mock"
    assert record.confidence is not None
    assert record.verdict == "HOLD" and record.risk_score is None


def test_midnight_distance():
    assert circular_hour_distance([23, 0, 1], 0) == 0
    assert circular_hour_distance([23, 0, 1], 2) == 1
    assert circular_hour_distance([9, 10, 11], 12) == 1


def registry(monkeypatch, p):
    rows = [
        dict(key="a" * 32, subject="owner", role="owner", owner_id=p.owner_human_id),
        dict(key="b" * 32, subject="agent", role="agent", agent_ids=[p.agent_id]),
        dict(key="c" * 32, subject="outsider", role="owner", owner_id="other"),
        dict(
            key="d" * 32, subject="reviewer", role="reviewer", owner_id=p.owner_human_id
        ),
    ]
    monkeypatch.setenv("SENTINEL_CREDENTIALS", json.dumps(rows))
    return rows


def test_role_and_owner_isolation(db, monkeypatch):
    p = policy(db)
    registry(monkeypatch, p)
    with TestClient(app) as c:
        assert (
            c.post(
                "/policies",
                headers={"X-API-Key": "b" * 32},
                json={"agent_id": p.agent_id, "owner_human_id": p.owner_human_id},
            ).status_code
            == 403
        )
        assert (
            c.get(
                "/policies/" + p.agent_id, headers={"X-API-Key": "c" * 32}
            ).status_code
            == 403
        )
        assert c.get("/policies", headers={"X-API-Key": "c" * 32}).json() == []
        assert (
            c.post(
                "/transactions/evaluate",
                headers={"X-API-Key": "d" * 32},
                json=dict(
                    request_id="req_" + uid(),
                    agent_id=p.agent_id,
                    merchant_name="m",
                    merchant_category="grocery",
                    amount=10,
                    stated_intent="buy groceries",
                ),
            ).status_code
            == 403
        )


def test_http_only_session_csrf_and_revocation(db, monkeypatch):
    p = policy(db)
    rows = registry(monkeypatch, p)
    origin = {"Origin": "http://localhost:5173"}
    with TestClient(app) as c:
        login = c.post("/auth/login", json={"credential": "a" * 32}, headers=origin)
        assert (
            login.status_code == 200
            and "httponly" in login.headers["set-cookie"].lower()
        )
        assert c.get("/auth/me").status_code == 200
        body = {"agent_id": p.agent_id, "owner_human_id": p.owner_human_id}
        assert c.post("/policies", json=body, headers=origin).status_code == 403
        assert (
            c.post(
                "/policies", json=body, headers=dict(origin, **{"X-Sentinel-CSRF": "1"})
            ).status_code
            == 200
        )
        monkeypatch.setenv("SENTINEL_CREDENTIALS", json.dumps(rows[1:]))
        assert c.get("/auth/me").status_code == 401


def test_unsigned_ap2_cannot_execute():
    with TestClient(app) as c:
        assert (
            c.post(
                "/ap2/evaluate-mandate",
                headers={"X-API-Key": "test-key-contracts"},
                json={},
            ).status_code
            == 501
        )


def test_batch_boundaries():
    with TestClient(app) as c:
        headers = {"X-API-Key": "test-key-contracts"}
        assert (
            c.post(
                "/transactions/evaluate-batch", headers=headers, json={"requests": []}
            ).status_code
            == 422
        )


def test_signed_capture_webhook_and_duplicate(db, monkeypatch):
    p = policy(db)
    req = request(p)
    SentinelOrchestrator(db).evaluate_transaction(p, req)
    payment = db.get(PaymentRecord, req.request_id)
    event = {
        "event": "payment.captured",
        "payload": {
            "payment": {
                "entity": {
                    "order_id": payment.order_id,
                    "amount": payment.amount_paise,
                    "currency": "INR",
                }
            }
        },
    }
    raw = json.dumps(event).encode()
    monkeypatch.setenv("RAZORPAY_WEBHOOK_SECRET", "webhook-test-secret")
    headers = {
        "X-Razorpay-Signature": hmac.new(
            b"webhook-test-secret", raw, hashlib.sha256
        ).hexdigest(),
        "X-Razorpay-Event-Id": uid(),
    }
    with TestClient(app) as c:
        assert c.post("/payments/webhook", content=raw).status_code == 401
        assert (
            c.post("/payments/webhook", content=raw, headers=headers).json()["status"]
            == "recorded"
        )
        assert (
            c.post("/payments/webhook", content=raw, headers=headers).json()["status"]
            == "duplicate"
        )
    db.expire_all()
    assert db.get(PaymentRecord, req.request_id).state == "captured"


def test_live_gateway_key_rejected(monkeypatch):
    from app.core.razorpay_gateway import RazorpayGateway

    monkeypatch.setenv("RAZORPAY_KEY_ID", "rzp_live_not_allowed")
    with pytest.raises(RuntimeError):
        RazorpayGateway()


def test_budget_history_is_not_truncated_to_200(db):
    p = policy(db, max_daily_total=2150)
    now = datetime.now(timezone.utc)
    for i in range(210):
        rid = "past_" + uid()
        db.add(
            TransactionRecord(
                request_id=rid,
                agent_id=p.agent_id,
                merchant_name="FreshMart",
                merchant_category="grocery",
                amount=10,
                requested_at_hour=now.hour,
                requested_at_iso=(now - timedelta(seconds=211 - i)).isoformat(),
                stated_intent="buy weekly groceries",
                raw_agent_goal=p.authorized_goal,
            )
        )
        db.flush()
        db.add(
            DecisionRecord(
                request_id=rid,
                verdict="APPROVE",
                reasons=["accepted"],
                checked_rules=[],
                decided_by="rule_engine",
            )
        )
        db.add(
            PaymentRecord(
                request_id=rid,
                state="mock",
                amount_paise=1000,
                budget_day=now.date().isoformat(),
            )
        )
    db.commit()
    d = SentinelOrchestrator(db).evaluate_transaction(p, request(p, amount=100))
    assert d.verdict == "BLOCK" and "daily cap" in d.reasons[0]


def test_dispatch_claim_is_atomic_across_threads(db):
    p = policy(db)
    req = request(p)
    o = SentinelOrchestrator(db)
    o.evaluate_transaction(p, req, execute_payment=False)
    db.add(
        PaymentRecord(request_id=req.request_id, state="pending", amount_paise=50000)
    )
    db.commit()
    calls = []

    def dispatch(_):
        local = SessionLocal()
        try:
            worker = SentinelOrchestrator(local)
            worker.gateway.create_order = lambda *args, **kwargs: (
                calls.append(req.request_id)
                or {"status": "mock", "order_id": "atomic_" + uid()}
            )
            return worker.dispatch_payment(req.request_id)
        finally:
            local.close()

    with ThreadPoolExecutor(max_workers=2) as pool:
        list(pool.map(dispatch, range(2)))
    assert len(calls) == 1


def test_added_purpose_does_not_use_keyword_fast_path(db):
    p = policy(db)
    d = SentinelOrchestrator(db).evaluate_transaction(
        p, request(p, stated_intent="buy weekly groceries for an office party")
    )
    assert d.decided_by == "llm_reasoner" and d.verdict == "HOLD"

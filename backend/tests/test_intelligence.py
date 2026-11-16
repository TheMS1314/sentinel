"""Risk signals cannot expand authority; emergency dispatch limits serialize globally."""

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
import json
from unittest.mock import MagicMock
import uuid

from fastapi.testclient import TestClient
import pytest
from app.api.main import app
from app.core.intelligence import assess, merchant_key
from app.core.intelligence_schemas import normalize_domain, IndicatorIn
from app.core.orchestrator import SentinelOrchestrator, ConflictError
from app.models.db import (
    Base,
    engine,
    SessionLocal,
    MerchantRiskProfile,
    ThreatIndicator,
    IntelligenceAssessment,
    DispatchGuard,
    DispatchClaim,
    RiskControlEvent,
    PaymentRecord,
    PolicyRecord,
)
from app.models.policy import AgentPolicy, TransactionRequest, VelocityLimit


def uid():
    return uuid.uuid4().hex


@pytest.fixture
def db():
    Base.metadata.create_all(engine)
    session = SessionLocal()
    for model in (
        DispatchGuard,
        DispatchClaim,
        MerchantRiskProfile,
        ThreatIndicator,
        RiskControlEvent,
    ):
        session.query(model).delete()
    session.commit()
    try:
        yield session
    finally:
        session.rollback()
        for model in (
            DispatchGuard,
            DispatchClaim,
            MerchantRiskProfile,
            ThreatIndicator,
            RiskControlEvent,
        ):
            session.query(model).delete()
        session.commit()
        session.close()


def make_policy(db, owner=None):
    aid = uid()
    p = AgentPolicy(
        "p_" + aid,
        "a_" + aid,
        owner or "owner_" + aid,
        velocity=VelocityLimit(100, 1440),
    )
    db.add(
        PolicyRecord(
            policy_id=p.policy_id,
            agent_id=p.agent_id,
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
        request_id="r_" + uid(),
        agent_id=p.agent_id,
        merchant_name="FreshMart",
        merchant_category="grocery",
        amount=29.29,
        requested_at_hour=now.hour,
        requested_at_iso=now.isoformat(),
        stated_intent=p.authorized_goal,
        raw_agent_goal=p.authorized_goal,
    )
    data.update(changes)
    return TransactionRequest(**data)


def profile(db, p, **changes):
    data = dict(
        owner_id=p.owner_human_id,
        merchant_name="FreshMart",
        merchant_key=merchant_key("FreshMart"),
        domain="shop.example",
        cyber_risk_score=10,
        source="operator",
        evidence="Reviewed evidence",
        expires_at=datetime.now(timezone.utc) + timedelta(days=1),
        updated_by="admin",
    )
    data.update(changes)
    row = MerchantRiskProfile(**data)
    db.add(row)
    db.commit()
    return row


def indicator(db, **changes):
    data = dict(
        indicator_id="i_" + uid(),
        domain="shop.example",
        source="operator",
        evidence="Confirmed by operator",
        confirmed=True,
        active=True,
        expires_at=datetime.now(timezone.utc) + timedelta(days=1),
        updated_by="admin",
    )
    data.update(changes)
    row = ThreatIndicator(**data)
    db.add(row)
    db.commit()
    return row


def pending(db, p, amount=29.29):
    o = SentinelOrchestrator(db)
    o.gateway.create_order = MagicMock(
        return_value={"status": "mock", "order_id": "mock_" + uid()}
    )
    o.dispatch_payment = MagicMock()
    req = request(p, amount=amount)
    assert o.evaluate_transaction(p, req).verdict == "APPROVE"
    return req, o


@pytest.mark.parametrize(
    "value",
    [
        "https://shop.example/path",
        "shop.example.evil/path",
        "*.example.com",
        "127.0.0.1",
        "host",
        "a..example",
        "a:443.example",
        "-bad.example",
    ],
)
def test_invalid_domain(value):
    with pytest.raises(ValueError):
        normalize_domain(value)


def test_canonical_domain_and_merchant_identity():
    assert normalize_domain("SHOP.Example.") == "shop.example"
    assert normalize_domain("bücher.example") == "xn--bcher-kva.example"
    assert merchant_key("  ＦＲＥＳＨＭＡＲＴ  ") == "freshmart"


def test_unmapped_identity_is_unknown_and_cannot_bypass_policy(db):
    p = make_policy(db)
    result = assess(db, p, request(p))
    assert result["vendor"]["score"] is None
    assert result["coverage"] == "merchant_identity_unmapped"
    assert all(v == "not_connected" for v in result["providers"].values())
    assert (
        SentinelOrchestrator(db)
        .evaluate_transaction(p, request(p, amount=99999))
        .verdict
        == "BLOCK"
    )


@pytest.mark.parametrize("score,expired", [(80, False), (10, True)])
def test_vendor_high_risk_or_stale_evidence_holds(db, score, expired):
    p = make_policy(db)
    profile(
        db,
        p,
        cyber_risk_score=score,
        expires_at=datetime.now(timezone.utc) + timedelta(days=-1 if expired else 1),
    )
    req = request(p)
    decision = SentinelOrchestrator(db).evaluate_transaction(p, req)
    assert decision.verdict == "HOLD"
    assert db.get(PaymentRecord, req.request_id) is None
    assert db.get(IntelligenceAssessment, req.request_id).report["vendor"][
        "status"
    ] == ("stale" if expired else "current")


def test_confirmed_threat_blocks_canonical_merchant(db):
    p = make_policy(db)
    profile(db, p)
    indicator(db)
    req = request(p, merchant_name="  ＦｒｅｓｈＭａｒｔ ")
    assert SentinelOrchestrator(db).evaluate_transaction(p, req).verdict == "BLOCK"
    assert db.get(PaymentRecord, req.request_id) is None


@pytest.mark.parametrize("synthetic_mapping", [False, True])
def test_synthetic_evidence_never_claims_confirmed_block(db, synthetic_mapping):
    p = make_policy(db)
    profile(db, p, source="synthetic" if synthetic_mapping else "operator")
    indicator(db, source="operator" if synthetic_mapping else "synthetic")
    assert (
        SentinelOrchestrator(db).evaluate_transaction(p, request(p)).verdict == "HOLD"
    )


def test_domain_match_is_exact_and_owner_scoped(db):
    p = make_policy(db)
    profile(db, p, domain="shop.example.evil")
    indicator(db)
    assert assess(db, p, request(p))["recommendation"] == "PASS"
    other = make_policy(db)
    assert assess(db, other, request(other))["coverage"] == "merchant_identity_unmapped"


def test_expired_indicator_is_stale_not_confirmed(db):
    p = make_policy(db)
    profile(db, p)
    indicator(db, expires_at=datetime.now(timezone.utc) - timedelta(seconds=1))
    report = assess(db, p, request(p))
    assert report["recommendation"] == "HOLD"
    assert report["threat"]["status"] == "stale"
    assert report["threat"]["matches"] == []


def test_relation_signal_is_owner_scoped_and_not_fraud_probability(db):
    owner = uid()
    ps = [make_policy(db, owner) for _ in range(3)]
    for p in ps[:2]:
        for _ in range(2):
            SentinelOrchestrator(db).evaluate_transaction(p, request(p))
    profile(db, ps[2])
    report = assess(db, ps[2], request(ps[2]))
    assert report["recommendation"] == "HOLD"
    assert report["relations"]["agents_including_current"] == 3
    assert report["relations"]["calibrated"] is False
    other = make_policy(db)
    profile(db, other)
    assert assess(db, other, request(other))["relations"]["alert"] is False


def test_freeze_after_approval_preserves_reservation_and_resume_is_idempotent(db):
    p = make_policy(db)
    req, o = pending(db, p)
    guard = DispatchGuard(
        id=1, frozen=True, reason="Incident investigation", updated_by="admin"
    )
    db.add(guard)
    db.commit()
    assert (
        SentinelOrchestrator.dispatch_payment(o, req.request_id)["status"]
        == "guard_hold"
    )
    assert db.get(PaymentRecord, req.request_id).state == "pending"
    o.gateway.create_order.assert_not_called()
    guard.frozen = False
    db.commit()
    assert SentinelOrchestrator.dispatch_payment(o, req.request_id)["status"] == "mock"
    assert SentinelOrchestrator.dispatch_payment(o, req.request_id)["status"] == "mock"
    assert o.gateway.create_order.call_count == 1
    assert db.query(DispatchClaim).filter_by(request_id=req.request_id).count() == 1


def test_new_confirmed_threat_stops_already_approved_pending_payment(db):
    p = make_policy(db)
    req, o = pending(db, p)
    profile(db, p)
    indicator(db)
    assert (
        SentinelOrchestrator.dispatch_payment(o, req.request_id)["status"]
        == "guard_hold"
    )
    o.gateway.create_order.assert_not_called()
    assert (
        db.get(IntelligenceAssessment, req.request_id).report["coverage"]
        == "merchant_identity_unmapped"
    )


def test_review_cannot_override_new_confirmed_threat(db):
    p = make_policy(db)
    req = request(p, stated_intent="unclear purpose")
    o = SentinelOrchestrator(db)
    assert o.evaluate_transaction(p, req).verdict == "HOLD"
    profile(db, p)
    indicator(db)
    with pytest.raises(ConflictError):
        o.resolve_hold(req.request_id, "APPROVE")


def test_global_count_limit_serializes_different_agents(db):
    requests = [pending(db, make_policy(db))[0] for _ in range(2)]
    db.add(
        DispatchGuard(id=1, max_dispatches=1, reason="Count guard", updated_by="admin")
    )
    db.commit()

    def dispatch(req):
        local = SessionLocal()
        try:
            o = SentinelOrchestrator(local)
            o.gateway.create_order = MagicMock(
                return_value={"status": "mock", "order_id": "mock_" + uid()}
            )
            return o.dispatch_payment(req.request_id)["status"]
        finally:
            local.close()

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(dispatch, requests))
    assert sorted(results) == ["guard_hold", "mock"]
    assert db.query(DispatchClaim).count() == 1


def test_amount_limit_uses_exact_paise_and_counts_failed_claims(db):
    p = make_policy(db)
    first, o = pending(db, p)
    second, o2 = pending(db, p)
    db.add(
        DispatchGuard(
            id=1, max_amount_paise=5857, reason="Exact money guard", updated_by="admin"
        )
    )
    db.commit()
    o.gateway.create_order.return_value = {"status": "failed"}
    assert (
        SentinelOrchestrator.dispatch_payment(o, first.request_id)["status"] == "failed"
    )
    assert (
        SentinelOrchestrator.dispatch_payment(o2, second.request_id)["status"]
        == "guard_hold"
    )
    o2.gateway.create_order.assert_not_called()
    claim = db.get(DispatchClaim, first.request_id)
    claim.claimed_at = datetime.now(timezone.utc) - timedelta(seconds=61)
    db.commit()
    assert (
        SentinelOrchestrator.dispatch_payment(o2, second.request_id)["status"] == "mock"
    )


def test_guard_api_is_admin_only_versioned_and_audited(db, monkeypatch):
    key = "o" * 40
    monkeypatch.setenv(
        "SENTINEL_CREDENTIALS",
        json.dumps(
            [{"key": key, "subject": "owner", "role": "owner", "owner_id": "someone"}]
        ),
    )
    c = TestClient(app)
    assert c.get("/risk/overview", headers={"X-API-Key": key}).status_code == 403
    assert (
        c.post(
            "/risk/guard",
            headers={"X-API-Key": key},
            json={"expected_version": 0, "frozen": True, "reason": "x"},
        ).status_code
        == 403
    )
    monkeypatch.setenv("SENTINEL_CREDENTIALS", "[]")
    headers = {"X-API-Key": "test-key-contracts"}
    body = {"expected_version": 0, "frozen": True, "reason": "Investigating"}
    assert c.post("/risk/guard", headers=headers, json=body).status_code == 200
    assert c.post("/risk/guard", headers=headers, json=body).status_code == 409
    assert db.query(RiskControlEvent).filter_by(kind="dispatch_guard").count() == 1
    assert c.get("/risk/overview", headers=headers).json()["guard"]["frozen"] is True


def test_evidence_api_scopes_snapshot_and_rejects_fake_provider_labels(db, monkeypatch):
    p = make_policy(db)
    c = TestClient(app)
    headers = {"X-API-Key": "test-key-contracts"}
    common = {
        "domain": "SHOP.Example.",
        "source": "operator",
        "evidence": "Reviewed",
        "expires_at": (datetime.now(timezone.utc) + timedelta(days=1)).isoformat(),
    }
    body = dict(
        common,
        owner_id=p.owner_human_id,
        merchant_name="FreshMart",
        cyber_risk_score=80,
    )
    assert c.post("/risk/merchants", headers=headers, json=body).status_code == 200
    req = request(p)
    assert SentinelOrchestrator(db).evaluate_transaction(p, req).verdict == "HOLD"
    assert (
        c.get("/risk/assessments/" + req.request_id, headers=headers).json()["vendor"][
            "score"
        ]
        == 80
    )
    assert (
        c.post(
            "/risk/indicators",
            headers=headers,
            json=dict(common, source="recorded_future", indicator_id="fake"),
        ).status_code
        == 422
    )
    key = "a" * 40
    monkeypatch.setenv(
        "SENTINEL_CREDENTIALS",
        json.dumps(
            [
                {
                    "key": key,
                    "subject": "unrelated",
                    "role": "agent",
                    "agent_ids": ["other"],
                }
            ]
        ),
    )
    assert (
        c.get(
            "/risk/assessments/" + req.request_id, headers={"X-API-Key": key}
        ).status_code
        == 403
    )


def test_evidence_requires_future_aware_expiry():
    with pytest.raises(ValueError):
        IndicatorIn(
            indicator_id="x",
            domain="shop.example",
            evidence="x",
            expires_at=datetime.now(),
        )

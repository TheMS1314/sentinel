"""Server-owned merchant attributes, version fencing and source provenance."""

from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock
import uuid
import pytest
from fastapi.testclient import TestClient
from app.api.main import app
from app.core.orchestrator import SentinelOrchestrator
from app.core.schemas import TransactionIn
from app.models.db import (
    Base,
    engine,
    SessionLocal,
    MerchantIdentity,
    PaymentRecord,
    IntelligenceAssessment,
    ThreatIndicator,
)
from app.models.policy import Decision
from test_intelligence import make_policy, request, profile, indicator


@pytest.fixture
def db():
    Base.metadata.create_all(engine)
    with SessionLocal() as session:
        session.query(ThreatIndicator).delete()
        session.query(MerchantIdentity).delete()
        session.commit()
        yield session
        session.rollback()
        session.query(ThreatIndicator).delete()
        session.query(MerchantIdentity).delete()
        session.commit()


def identity(db, p, **changes):
    data = dict(
        merchant_id="mid_" + uuid.uuid4().hex,
        owner_id=p.owner_human_id,
        legal_name="CanonicalShop",
        category="grocery",
        domain="canonical.example",
        operator_reference="Operator approved directory record",
        active=True,
        version=1,
        approved_by="local-admin",
    )
    data.update(changes)
    row = MerchantIdentity(**data)
    db.add(row)
    db.commit()
    return row


def test_domain_mismatch_never_softens_confirmed_block_or_erases_evidence(db):
    from app.core.intelligence import merchant_key

    p = make_policy(db)
    m = identity(db, p)
    profile(
        db,
        p,
        merchant_name=m.legal_name,
        merchant_key=merchant_key(m.legal_name),
        domain="previous.example",
    )
    threat = indicator(db, domain="previous.example")
    req = request(p, merchant_id=m.merchant_id)
    decision = SentinelOrchestrator(db).evaluate_transaction(p, req)
    assert decision.verdict == "BLOCK"
    assert db.get(PaymentRecord, req.request_id) is None
    evidence = (
        db.query(IntelligenceAssessment)
        .filter_by(request_id=req.request_id)
        .one()
        .report
    )
    assert evidence["recommendation"] == "BLOCK"
    assert (
        evidence["profile_domain_threat"]["matches"][0]["indicator_id"]
        == threat.indicator_id
    )
    assert any("differs" in s for s in evidence["signals"])
    assert any("Operator-confirmed" in s for s in evidence["signals"])


def test_id_overrides_agent_merchant_attributes_and_preserves_canonical_record(db):
    p = make_policy(db)
    m = identity(db, p, category="casino")
    req = request(
        p,
        merchant_id=m.merchant_id,
        merchant_name="Grocery Shop",
        merchant_category="grocery",
    )
    result = SentinelOrchestrator(db).evaluate_transaction(p, req)
    assert (
        result.verdict == "BLOCK"
        and req.merchant_name == "CanonicalShop"
        and req.merchant_category == "casino"
    )
    assert db.get(PaymentRecord, req.request_id) is None


@pytest.mark.parametrize("case", ["unknown", "another_owner", "inactive"])
def test_unknown_out_of_scope_or_inactive_identity_cannot_approve(db, case):
    p = make_policy(db)
    m = identity(
        db,
        p,
        owner_id="other" if case == "another_owner" else p.owner_human_id,
        active=case != "inactive",
    )
    req = request(
        p,
        merchant_id="missing_" + uuid.uuid4().hex
        if case == "unknown"
        else m.merchant_id,
    )
    result = SentinelOrchestrator(db).evaluate_transaction(p, req)
    assert result.verdict == "BLOCK" and "merchant_identity" in result.checked_rules


def test_directory_change_pauses_pending_and_does_not_rewrite_replay(db):
    p = make_policy(db)
    m = identity(db, p)
    o = SentinelOrchestrator(db)
    o.dispatch_payment = MagicMock()
    req = request(p, merchant_id=m.merchant_id)
    original = o.evaluate_transaction(p, req)
    m.version = 2
    m.legal_name = "UpdatedShop"
    db.commit()
    assert (
        SentinelOrchestrator.dispatch_payment(o, req.request_id)["status"]
        == "guard_hold"
    )
    replay = request(
        p,
        request_id=req.request_id,
        merchant_id=m.merchant_id,
        requested_at_iso=req.requested_at_iso,
        requested_at_hour=req.requested_at_hour,
    )
    assert o.evaluate_transaction(p, replay).verdict == original.verdict == "APPROVE"
    snapshot = db.get(IntelligenceAssessment, req.request_id).report
    assert (
        snapshot["identity"]["version"] == 1
        and snapshot["identity"]["settlement_verified"] is False
    )


def test_directory_domain_is_used_without_a_vendor_profile(db):
    p = make_policy(db)
    m = identity(db, p)
    db.add(
        ThreatIndicator(
            indicator_id=uuid.uuid4().hex,
            domain=m.domain,
            source="operator",
            evidence="Confirmed threat",
            confirmed=True,
            active=True,
            expires_at=datetime.now(timezone.utc) + timedelta(days=1),
            updated_by="local-admin",
        )
    )
    db.commit()
    req = request(p, merchant_id=m.merchant_id)
    assert SentinelOrchestrator(db).evaluate_transaction(p, req).verdict == "BLOCK"


def test_changed_directory_discards_old_semantic_approval(db):
    p = make_policy(db)
    m = identity(db, p)
    mid = m.merchant_id
    req = request(p, merchant_id=mid, stated_intent="groceries for dinner")
    o = SentinelOrchestrator(db)

    def model(policy, prepared):
        assert not db.in_transaction()
        with SessionLocal() as other:
            row = other.get(MerchantIdentity, mid)
            row.version += 1
            row.legal_name = "New legal name"
            other.commit()
        return Decision(
            prepared.request_id,
            "APPROVE",
            ["Synthetic model response"],
            [],
            "llm_reasoner",
        )

    o.reasoner.evaluate = model
    result = o.evaluate_transaction(p, req)
    assert result.verdict == "HOLD", result.reasons
    assert "merchant_identity_changed" in result.checked_rules


def test_merchant_api_audits_version_and_forbids_owner_reassignment(db):
    p = make_policy(db)
    c = TestClient(app)
    headers = {"X-API-Key": "test-key-contracts"}
    body = dict(
        owner_id=p.owner_human_id,
        legal_name="Shop",
        category="grocery",
        domain="shop.example",
        operator_reference="Human register",
        expected_version=0,
    )
    created = c.post("/merchants", json=body, headers=headers)
    assert created.status_code == 200
    row = created.json()
    assert (
        row["verification_level"] == "operator_attested"
        and row["settlement_verified"] is False
    )
    body.update(
        merchant_id=row["merchant_id"], expected_version=1, owner_id="new-owner"
    )
    assert c.post("/merchants", json=body, headers=headers).status_code == 403
    body.update(owner_id=p.owner_human_id, expected_version=0)
    assert c.post("/merchants", json=body, headers=headers).status_code == 409


def test_production_input_requires_stable_registered_merchant_id(monkeypatch):
    monkeypatch.setenv("SENTINEL_ENV", "production")
    with pytest.raises(ValueError):
        TransactionIn(
            request_id="req_x",
            agent_id="a",
            merchant_name="m",
            merchant_category="grocery",
            amount=1,
            stated_intent="buy",
        )
    valid = TransactionIn(
        request_id="req_x",
        agent_id="a",
        merchant_id="mid_x",
        amount=1,
        stated_intent="buy",
    )
    assert valid.merchant_name == "unresolved"

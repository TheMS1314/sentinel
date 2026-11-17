"""Shared revocation, scope/version fencing and one-time bootstrap across sessions."""

import json
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch
import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient
from app.api.main import app, _process_single_evaluation
from app.api.authority import CredentialChange
from app.core.auth import authenticate, principal_for_subject
from app.core.credential_admin import bootstrap, change
from app.core.orchestrator import SentinelOrchestrator, ConflictError
from app.models.db import (
    Base,
    engine,
    SessionLocal,
    ServiceCredential,
    RiskControlEvent,
)
from test_intelligence import make_policy, request

KEY = "initial-admin-key-" + "x" * 32


@pytest.fixture
def db(monkeypatch):
    Base.metadata.create_all(engine)
    monkeypatch.setenv("SENTINEL_AUTH_BACKEND", "database")
    monkeypatch.setenv(
        "SENTINEL_CREDENTIALS",
        json.dumps([{"key": KEY, "subject": "shared-admin", "role": "admin"}]),
    )
    with SessionLocal() as session:
        session.query(ServiceCredential).delete()
        session.commit()
        bootstrap(session)
        yield session
        session.rollback()
        session.query(ServiceCredential).delete()
        session.commit()


def admin():
    return authenticate(KEY)


def test_credential_expiry_uses_authority_clock_even_when_replica_clock_lags(db):
    from app.core.auth import assert_current_principal

    principal = admin()
    expires = datetime.now(timezone.utc) + timedelta(days=1)
    db.get(ServiceCredential, "shared-admin").expires_at = expires
    db.commit()
    with patch(
        "app.core.auth.processing_time", return_value=expires + timedelta(seconds=1)
    ):
        with pytest.raises(HTTPException):
            authenticate(KEY)
        with pytest.raises(HTTPException):
            assert_current_principal(principal, db)


def test_browser_session_issue_and_expiry_share_database_clock(db):
    from app.models.db import BrowserSession
    from app.core.auth import digest

    trusted = datetime.now(timezone.utc) - timedelta(days=2)
    client = TestClient(app)
    with patch("app.api.main.processing_time", return_value=trusted):
        response = client.post(
            "/auth/login",
            json={"credential": KEY},
            headers={"Origin": "http://localhost:5173"},
        )
    assert response.status_code == 200
    row = db.get(BrowserSession, digest(client.cookies.get("sentinel_session")))
    assert row.expires_at.replace(tzinfo=timezone.utc) == trusted + timedelta(hours=8)
    with patch(
        "app.core.auth.processing_time", return_value=trusted + timedelta(hours=7)
    ):
        assert client.get("/auth/me").status_code == 200
    with patch(
        "app.core.auth.processing_time", return_value=trusted + timedelta(hours=9)
    ):
        assert client.get("/auth/me").status_code == 401


def action(db, subject, kind, version, **fields):
    return change(
        db,
        admin(),
        CredentialChange(
            subject=subject, action=kind, expected_version=version, **fields
        ),
    )


def test_bootstrap_hashes_keys_and_never_resurrects_revoked_authority(db, monkeypatch):
    row = db.get(ServiceCredential, "shared-admin")
    assert row.key_hash != KEY and len(row.key_hash) == 64
    other = action(db, "second-admin", "create", 0, role="admin")
    action(db, "second-admin", "revoke", 1)
    monkeypatch.setenv(
        "SENTINEL_CREDENTIALS",
        json.dumps(
            [{"key": other["credential"], "subject": "second-admin", "role": "admin"}]
        ),
    )
    assert bootstrap(db) is False
    with pytest.raises(HTTPException):
        authenticate(other["credential"])


def test_rotation_is_immediate_across_independent_sessions_and_revokes_cookie(db):
    created = action(db, "human", "create", 0, role="owner", owner_id="o")
    client = TestClient(app)
    response = client.post(
        "/auth/login",
        json={"credential": created["credential"]},
        headers={"Origin": "http://localhost:5173"},
    )
    assert response.status_code == 200
    assert client.get("/auth/me").status_code == 200
    rotated = action(db, "human", "rotate", 1)
    with SessionLocal() as other:
        assert other.get(ServiceCredential, "human").version == 2
    with pytest.raises(HTTPException):
        authenticate(created["credential"])
    assert authenticate(rotated["credential"]).credential_version == 2
    assert client.get("/auth/me").status_code == 401


def test_plaintext_keys_are_not_in_list_or_audit_and_stale_mutation_conflicts(db):
    issued = action(db, "robot", "create", 0, role="agent", agent_ids=["a"])
    c = TestClient(app)
    headers = {"X-API-Key": KEY}
    listed = c.get("/credentials", headers=headers)
    assert (
        listed.status_code == 200
        and issued["credential"] not in listed.text
        and "key_hash" not in listed.text
    )
    audit = [r.after for r in db.query(RiskControlEvent).filter_by(entity_id="robot")]
    assert issued["credential"] not in json.dumps(audit)
    with pytest.raises(HTTPException) as error:
        action(db, "robot", "rotate", 0)
    assert error.value.status_code == 409


def test_last_administrator_cannot_be_revoked_or_demoted(db):
    for kind, extra in [("revoke", {}), ("scope", {"role": "owner", "owner_id": "o"})]:
        with pytest.raises(HTTPException) as error:
            action(db, "shared-admin", kind, 1, **extra)
        assert error.value.status_code == 409


def test_rotation_fences_old_queued_commands_and_pending_dispatch(db):
    p = make_policy(db)
    robot = action(db, "robot", "create", 0, role="agent", agent_ids=[p.agent_id])
    req = request(p)
    o = SentinelOrchestrator(db)
    o.dispatch_payment = MagicMock()
    o.evaluate_transaction(p, req, actor_id="robot", actor_version=1)
    action(db, "robot", "rotate", 1)
    assert (
        SentinelOrchestrator.dispatch_payment(o, req.request_id)["status"]
        == "guard_hold"
    )
    with pytest.raises(ValueError):
        _process_single_evaluation(
            dict(request(p).__dict__, _submitted_by="robot", _submitted_version=1)
        )
    with pytest.raises(ConflictError):
        o.evaluate_transaction(p, request(p), actor_id="robot", actor_version=1)
    with pytest.raises(HTTPException):
        authenticate(robot["credential"])


def test_expired_credentials_are_not_authenticated(db):
    result = action(db, "temporary", "create", 0, role="owner", owner_id="o")
    row = db.get(ServiceCredential, "temporary")
    row.expires_at = datetime.now(timezone.utc) - timedelta(seconds=1)
    db.commit()
    with pytest.raises(HTTPException):
        authenticate(result["credential"])
    assert principal_for_subject("temporary") is None


def test_non_admin_cannot_manage_credentials(db):
    result = action(db, "owner", "create", 0, role="owner", owner_id="o")
    client = TestClient(app)
    assert (
        client.get(
            "/credentials", headers={"X-API-Key": result["credential"]}
        ).status_code
        == 403
    )


def test_admin_issue_response_is_not_cached(db):
    c = TestClient(app)
    response = c.post(
        "/credentials",
        headers={"X-API-Key": KEY},
        json={
            "subject": "agent2",
            "action": "create",
            "expected_version": 0,
            "role": "agent",
            "agent_ids": ["a"],
        },
    )
    assert (
        response.status_code == 200 and response.headers["Cache-Control"] == "no-store"
    )


def test_existing_authority_does_not_require_old_bootstrap_material(db, monkeypatch):
    monkeypatch.delenv("SENTINEL_CREDENTIALS")
    assert bootstrap(db) is False


def test_reconciliation_rechecks_admin_version_after_provider_io(db):
    from app.models.db import PaymentRecord
    from app.core.auth import digest

    p = make_policy(db)
    req = request(p)
    o = SentinelOrchestrator(db)
    o.dispatch_payment = MagicMock()
    assert o.evaluate_transaction(p, req, actor_id="shared-admin").verdict == "APPROVE"

    def provider(*args, **kwargs):
        with SessionLocal() as other:
            credential = other.get(ServiceCredential, "shared-admin")
            credential.key_hash = digest("replacement-key-not-disclosed")
            credential.version += 1
            other.commit()
        return dict(order_id="order_contract", status="created")

    from unittest.mock import patch

    with patch(
        "app.core.razorpay_gateway.RazorpayGateway.reconcile", side_effect=provider
    ):
        result = TestClient(app).post(
            "/payments/" + req.request_id + "/reconcile",
            json={"order_id": "order_contract"},
            headers={"X-API-Key": KEY},
        )
    assert result.status_code == 401
    db.expire_all()
    assert db.get(PaymentRecord, req.request_id).state == "pending"

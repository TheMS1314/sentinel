"""Operational failures are durable, deduplicated and never reported delivered early."""

import hashlib
import hmac
import json
import uuid
from datetime import datetime, timedelta, timezone
from unittest.mock import patch
import pytest
from fastapi.testclient import TestClient
from app.api.main import app
from app.core.operations import (
    detect,
    open_incident,
    deliver_one,
    endpoint,
    claim_delivery,
)
from app.models.db import Base, engine, SessionLocal, Incident, AlertDelivery
from test_intelligence import make_policy, request
from app.core.orchestrator import SentinelOrchestrator


@pytest.fixture
def db(monkeypatch):
    monkeypatch.delenv("SENTINEL_ALERT_WEBHOOK_URL", raising=False)
    Base.metadata.create_all(engine)
    with SessionLocal() as s:
        s.query(AlertDelivery).delete()
        s.query(Incident).delete()
        s.commit()
        yield s
        s.rollback()
        s.query(AlertDelivery).delete()
        s.query(Incident).delete()
        s.commit()


def configured(monkeypatch):
    monkeypatch.setenv("SENTINEL_ALERT_WEBHOOK_URL", "https://alerts.example/events")
    monkeypatch.setenv("SENTINEL_ALERT_ALLOWED_HOSTS", "alerts.example")
    monkeypatch.setenv("SENTINEL_ALERT_SIGNING_SECRET", "test-signing-" + "x" * 32)


def test_dependency_incident_is_deduplicated_and_recovery_does_not_silence_a_case(db):
    detect(db, False)
    detect(db, False)
    row = db.query(Incident).filter_by(kind="quota_dependency").one()
    assert db.query(AlertDelivery).filter_by(incident_id=row.incident_id).count() == 1
    detect(db, True)
    db.refresh(row)
    assert row.state == "open" and not row.condition_active
    assert not deliver_one(db)
    assert (
        db.query(AlertDelivery).filter_by(incident_id=row.incident_id).one().state
        == "pending"
    )


def test_webhook_signing_retry_and_success(db, monkeypatch):
    configured(monkeypatch)
    row = open_incident(
        db, "manual_review", "req_private", "warning", "Private human text"
    )
    db.commit()
    event = db.query(AlertDelivery).filter_by(incident_id=row.incident_id).one()
    with patch("app.core.operations.requests.Session") as session:
        client = session.return_value.__enter__.return_value
        response = client.post.return_value.__enter__.return_value
        response.status_code = 503
        assert deliver_one(db)
        db.refresh(event)
        assert event.state == "pending" and event.attempts == 1
        call = client.post.call_args.kwargs
        raw = call["data"]
        assert b"Private human text" not in raw
        assert call["allow_redirects"] is False and call["stream"] is True
        assert hmac.compare_digest(
            call["headers"]["X-Sentinel-Signature"],
            hmac.new(
                ("test-signing-" + "x" * 32).encode(), raw, hashlib.sha256
            ).hexdigest(),
        )
        event.next_attempt_at = datetime.now(timezone.utc) - timedelta(seconds=1)
        db.commit()
        response.status_code = 204
        assert deliver_one(db)
        db.refresh(event)
        assert event.state == "delivered" and event.attempts == 2
        assert (
            client.post.call_args.kwargs["headers"]["X-Sentinel-Event-Id"]
            == call["headers"]["X-Sentinel-Event-Id"]
        )


def test_recovered_lease_is_fenced_and_retry_budget_is_terminal(db):
    row = open_incident(
        db, "manual_review", "req_" + uuid.uuid4().hex, "warning", "review"
    )
    db.commit()
    first = claim_delivery(db)
    event = db.get(AlertDelivery, first[0])
    event.lease_expires_at = datetime.now(timezone.utc) - timedelta(seconds=1)
    db.commit()
    second = claim_delivery(db)
    assert first[1] != second[1]
    event.attempts = 5
    event.lease_expires_at = datetime.now(timezone.utc) - timedelta(seconds=1)
    db.commit()
    assert claim_delivery(db) is None
    db.refresh(event)
    assert event.state == "failed" and row.incident_id == event.incident_id


@pytest.mark.parametrize(
    "url",
    [
        "http://alerts.example/e",
        "https://evil.example/e",
        "https://user:pass@alerts.example/e",
        "https://alerts.example/e#fragment",
    ],
)
def test_configured_sink_rejects_untrusted_transport(monkeypatch, url):
    configured(monkeypatch)
    monkeypatch.setenv("SENTINEL_ALERT_WEBHOOK_URL", url)
    with pytest.raises(ValueError):
        endpoint()


def test_administrator_lifecycle_conflicts_active_conditions_and_protected_metrics(db):
    detect(db, False)
    row = db.query(Incident).filter_by(kind="quota_dependency").one()
    c = TestClient(app)
    headers = {"X-API-Key": "test-key-contracts"}
    assert c.get("/ops/metrics").status_code == 401
    ack = dict(expected_version=1, state="acknowledged", reason="Investigating")
    assert (
        c.post(
            "/ops/incidents/" + row.incident_id, json=ack, headers=headers
        ).status_code
        == 200
    )
    assert (
        c.post(
            "/ops/incidents/" + row.incident_id, json=ack, headers=headers
        ).status_code
        == 409
    )
    ack.update(expected_version=2, state="resolved")
    assert (
        c.post(
            "/ops/incidents/" + row.incident_id, json=ack, headers=headers
        ).status_code
        == 409
    )
    detect(db, True)
    assert (
        c.post(
            "/ops/incidents/" + row.incident_id, json=ack, headers=headers
        ).status_code
        == 200
    )
    text = c.get("/ops/metrics", headers=headers).text
    assert 'sentinel_alert_deliveries{state="pending"}' in text
    assert row.incident_id not in text
    assert c.get("/ops/summary", headers=headers).json()["delivery"] == "not_configured"


def test_manual_case_requires_existing_request_and_redacts_outbound_summary(db):
    p = make_policy(db)
    req = request(p)
    SentinelOrchestrator(db).evaluate_transaction(p, req)
    c = TestClient(app)
    headers = {"X-API-Key": "test-key-contracts"}
    assert (
        c.post(
            "/ops/incidents",
            headers=headers,
            json={"request_id": "missing", "summary": "secret"},
        ).status_code
        == 404
    )
    result = c.post(
        "/ops/incidents",
        headers=headers,
        json={"request_id": req.request_id, "summary": "private case details"},
    )
    assert result.status_code == 200
    event = (
        db.query(AlertDelivery)
        .filter_by(incident_id=result.json()["incident_id"])
        .one()
    )
    assert "private case details" not in json.dumps(event.payload)
    event.state = "failed"
    db.commit()
    retry = c.post(
        "/ops/alerts/" + event.event_id + "/retry",
        headers=headers,
        json={"reason": "Destination fixed"},
    )
    assert retry.status_code == 200
    db.refresh(event)
    assert event.state == "pending" and event.attempts == 0


def test_bounded_monitor_scan_eventually_rechecks_later_incidents(db):
    # Persistently open manual cases must not starve recovery checks behind them.
    now = datetime.now(timezone.utc)
    for i in range(305):
        db.add(
            Incident(
                incident_id=f"case_{i:04d}",
                fingerprint="manual_" + str(i),
                kind="manual_review",
                entity_id="r" + str(i),
                severity="warning",
                state="open",
                condition_active=True,
                summary="Manual case",
                version=1,
                first_seen=now,
                last_seen=now,
            )
        )
    later = Incident(
        incident_id="z_later_dependency",
        fingerprint="laterquota",
        kind="quota_dependency",
        entity_id="redis",
        severity="warning",
        state="open",
        condition_active=True,
        summary="Recovered dependency",
        version=1,
        first_seen=now,
        last_seen=now,
    )
    db.add(later)
    db.commit()
    detect(db, True)
    db.refresh(later)
    assert later.condition_active
    detect(db, True)
    db.refresh(later)
    assert not later.condition_active and later.last_checked_at is not None

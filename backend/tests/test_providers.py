"""Fixture contracts are not licensed live API verification."""

from datetime import datetime, timedelta, timezone
import json
from unittest.mock import patch
import pytest
from fastapi.testclient import TestClient
from app.api.main import app
from app.core import providers
from app.core.orchestrator import SentinelOrchestrator
from app.models.db import (
    Base,
    engine,
    SessionLocal,
    ProviderBinding,
    ProviderEvidence,
    MerchantIdentity,
)
from test_intelligence import make_policy, request
from test_merchant_identity import identity


@pytest.fixture
def db(monkeypatch):
    Base.metadata.create_all(engine)
    monkeypatch.setenv("RECORDED_FUTURE_API_TOKEN", "")
    monkeypatch.setenv("RISKRECON_API_TOKEN", "")
    with SessionLocal() as s:
        yield s
        s.rollback()
        s.query(ProviderEvidence).delete()
        s.query(ProviderBinding).delete()
        s.query(MerchantIdentity).delete()
        s.commit()


def payload():
    return {
        "data": {
            "entity": {"id": "idn:canonical.example"},
            "risk": {"score": 20},
            "timestamps": {"lastSeen": datetime.now(timezone.utc).isoformat()},
        }
    }


def test_adapters_validate_entity_scale_and_timezone():
    p = payload()
    assert providers.normalize("recorded_future", "idn:canonical.example", p)[0] == 20
    with pytest.raises(ValueError):
        providers.normalize("recorded_future", "idn:other.example", p)
    p["data"]["risk"]["score"] = True
    with pytest.raises(ValueError):
        providers.normalize("recorded_future", "idn:canonical.example", p)
    rr = {
        "toes": [
            {
                "toe_id": "toe1",
                "overall_rating_numeric": 8.5,
                "last_analysis_ts": datetime.now(timezone.utc).isoformat(),
            }
        ]
    }
    assert providers.normalize("riskrecon", "toe1", rr)[0] == 8.5
    rr["toes"].append(rr["toes"][0])
    with pytest.raises(ValueError):
        providers.normalize("riskrecon", "toe1", rr)


def test_transport_is_bounded_and_source_age_never_becomes_fresh_on_refetch(
    monkeypatch,
):
    monkeypatch.setenv("RECORDED_FUTURE_API_TOKEN", "fixture-only")
    with patch("app.core.providers.requests.Session") as session:
        client = session.return_value.__enter__.return_value
        response = client.get.return_value.__enter__.return_value
        response.status_code = 200
        p = payload()
        p["data"]["timestamps"]["lastSeen"] = (
            datetime.now(timezone.utc) - timedelta(days=30)
        ).isoformat()
        response.iter_content.return_value = [json.dumps(p).encode()]
        result = providers.fetch("recorded_future", "idn:canonical.example")
        assert result["status"] == "stale" and result["response_hash"]
        kwargs = client.get.call_args.kwargs
        assert (
            kwargs["allow_redirects"] is False
            and kwargs["timeout"] == (3, 5)
            and client.trust_env is False
        )
        response.iter_content.return_value = [b"x" * 262145]
        assert (
            providers.fetch("recorded_future", "idn:canonical.example")["status"]
            == "error"
        )
        response.status_code = 302
        assert (
            providers.fetch("recorded_future", "idn:canonical.example")["error"]
            == "http_status_302"
        )


def test_missing_required_feed_holds_even_without_mapping(db, monkeypatch):
    p = make_policy(db)
    monkeypatch.setenv("SENTINEL_REQUIRED_FEEDS", "recorded_future")
    assert (
        SentinelOrchestrator(db).evaluate_transaction(p, request(p)).verdict == "HOLD"
    )


def test_binding_refresh_version_and_dispatch_recheck(db, monkeypatch):
    p = make_policy(db)
    m = identity(db, p)
    c = TestClient(app)
    h = {"X-API-Key": "test-key-contracts"}
    body = dict(
        merchant_id=m.merchant_id,
        provider="recorded_future",
        external_id="idn:" + m.domain,
        expected_version=0,
    )
    assert (
        c.post(
            "/providers/bindings",
            json=dict(body, external_id="idn:evil.example"),
            headers=h,
        ).status_code
        == 422
    )
    saved = c.post("/providers/bindings", json=body, headers=h)
    assert saved.status_code == 200
    bid = saved.json()["binding_id"]
    assert (
        c.post("/providers/bindings/" + bid + "/refresh", headers=h).json()["status"]
        == "not_configured"
    )
    assert (
        SentinelOrchestrator(db)
        .evaluate_transaction(p, request(p, merchant_id=m.merchant_id))
        .verdict
        == "HOLD"
    )
    now = datetime.now(timezone.utc)
    with patch(
        "app.core.providers.fetch",
        return_value=dict(
            status="current",
            score=10,
            observed_at=now,
            fetched_at=now,
            expires_at=now + timedelta(hours=1),
            response_hash="a" * 64,
            error=None,
        ),
    ):
        assert (
            c.post("/providers/bindings/" + bid + "/refresh", headers=h).json()[
                "status"
            ]
            == "current"
        )
    db.expire_all()
    o = SentinelOrchestrator(db)
    o.dispatch_payment = lambda _: None
    req = request(p, merchant_id=m.merchant_id)
    assert o.evaluate_transaction(p, req).verdict == "APPROVE"
    evidence = db.query(ProviderEvidence).filter_by(status="current").one()
    # The append-only record is changed here only to exercise the clock boundary.
    evidence.expires_at = now - timedelta(seconds=1)
    db.commit()
    assert (
        SentinelOrchestrator(db).dispatch_payment(req.request_id)["status"]
        == "guard_hold"
    )
    assert c.get("/providers").status_code == 401


def test_changed_binding_during_external_io_discards_the_response(db):
    p = make_policy(db)
    m = identity(db, p)
    c = TestClient(app)
    headers = {"X-API-Key": "test-key-contracts"}
    bid = c.post(
        "/providers/bindings",
        json=dict(
            merchant_id=m.merchant_id,
            provider="recorded_future",
            external_id="idn:" + m.domain,
            expected_version=0,
        ),
        headers=headers,
    ).json()["binding_id"]

    def remote(*args):
        with SessionLocal() as other:
            row = other.get(ProviderBinding, bid)
            row.version += 1
            other.commit()
        now = datetime.now(timezone.utc)
        return dict(
            status="current",
            score=0,
            observed_at=now,
            fetched_at=now,
            expires_at=now + timedelta(hours=1),
            response_hash="a" * 64,
            error=None,
        )

    with patch("app.core.providers.fetch", side_effect=remote):
        result = c.post("/providers/bindings/" + bid + "/refresh", headers=headers)
    assert result.status_code == 409
    assert db.query(ProviderEvidence).filter_by(binding_id=bid).count() == 0

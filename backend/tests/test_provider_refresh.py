"""No duplicate in-flight enrichment, no stale-writer evidence, no implicit opt-in."""

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from unittest.mock import patch
import pytest
from app.core.provider_refresh import claim, finish, run_one, RefreshConflict
from app.core.providers import assessment
from app.models.db import (
    ProviderBinding,
    ProviderEvidence,
    SessionLocal,
    MerchantIdentity,
)
from test_providers import db as db
from test_merchant_identity import identity
from test_intelligence import make_policy


def binding(db, **changes):
    p = make_policy(db)
    m = identity(db, p)
    data = dict(
        binding_id="binding_" + m.merchant_id,
        merchant_id=m.merchant_id,
        merchant_version=m.version,
        provider="recorded_future",
        external_id="idn:" + m.domain,
        updated_by="local-admin",
        version=1,
    )
    data.update(changes)
    b = ProviderBinding(**data)
    db.add(b)
    db.commit()
    return b, m


def response(**changes):
    now = datetime.now(timezone.utc)
    data = dict(
        status="current",
        score=10,
        observed_at=now,
        fetched_at=now,
        expires_at=now + timedelta(hours=1),
        response_hash="b" * 64,
        error=None,
    )
    data.update(changes)
    return data


def test_parallel_replica_claims_only_one_refresh(db):
    b, m = binding(db)
    bid = b.binding_id
    db.rollback()

    def worker(_):
        with SessionLocal() as other:
            try:
                return claim(other, bid)
            except RefreshConflict:
                return None

    with ThreadPoolExecutor(max_workers=4) as pool:
        claims = list(pool.map(worker, range(4)))
    assert sum(c is not None for c in claims) == 1
    db.expire_all()
    assert db.get(ProviderBinding, bid).refresh_generation == 1


def test_expired_lease_recovers_and_late_writer_cannot_replace_evidence(db):
    b, m = binding(db)
    bid = b.binding_id
    original = claim(db, bid)
    row = db.get(ProviderBinding, bid)
    row.refresh_lease_expires_at = datetime.now(timezone.utc) - timedelta(seconds=1)
    db.commit()
    recovered = claim(db, bid)
    assert (
        recovered.generation == original.generation + 1
        and recovered.token != original.token
    )
    finish(db, recovered, response(score=90))
    with pytest.raises(RefreshConflict):
        finish(
            db,
            original,
            response(
                score=0, fetched_at=datetime.now(timezone.utc) + timedelta(days=1)
            ),
        )
    assert db.query(ProviderEvidence).filter_by(binding_id=bid).count() == 1
    assert assessment(db, m)[0]["hold"] is True


def test_network_io_has_no_open_transaction_and_due_time_is_persisted(db):
    b, m = binding(db, auto_refresh=True)
    bid = b.binding_id

    def external(*_):
        assert not db.in_transaction()
        with SessionLocal() as other:
            with pytest.raises(RefreshConflict):
                claim(other, bid)
        return response(fetched_at=datetime(2000, 1, 1, tzinfo=timezone.utc))

    with patch("app.core.providers.fetch", side_effect=external):
        result = run_one(db, bid)
    row = db.get(ProviderBinding, bid)
    evidence = db.get(ProviderEvidence, result["evidence_id"])
    assert row.refresh_token is None and row.refresh_status == "current"
    assert row.refresh_next_at.replace(tzinfo=timezone.utc) > datetime.now(timezone.utc)
    assert evidence.fetched_at.year != 2000 and evidence.refresh_generation == 1


def test_scheduler_requires_global_and_binding_opt_in_and_respects_backoff(
    db, monkeypatch
):
    b, m = binding(db, auto_refresh=True)
    bid = b.binding_id
    with patch("app.core.providers.fetch") as external:
        assert run_one(db) is None
        external.assert_not_called()
    monkeypatch.setenv("SENTINEL_PROVIDER_REFRESH_ENABLED", "1")
    db.get(ProviderBinding, bid).auto_refresh = False
    db.commit()
    assert claim(db) is None
    db.get(ProviderBinding, bid).auto_refresh = True
    db.commit()
    with patch(
        "app.core.providers.fetch",
        return_value=response(
            status="not_configured",
            score=None,
            observed_at=None,
            error="Provider credential missing",
        ),
    ) as external:
        assert run_one(db)["status"] == "not_configured"
        assert run_one(db) is None
        assert external.call_count == 1
    assert assessment(db, m)[0]["hold"] is True


def test_domain_change_prevents_sending_old_entity_to_provider(db):
    b, m = binding(db)
    bid = b.binding_id
    m.domain = "new-domain.example"
    m.version += 1
    db.commit()
    with patch("app.core.providers.fetch") as external:
        assert run_one(db, bid) is None
        external.assert_not_called()
    assert db.get(ProviderBinding, bid).refresh_status == "binding_changed"


def test_source_watermark_survives_a_failed_refresh(db):
    b, m = binding(db)
    bid = b.binding_id
    observed = datetime.now(timezone.utc)
    finish(db, claim(db, bid), response(observed_at=observed))
    finish(
        db,
        claim(db, bid),
        response(status="error", score=None, observed_at=None, error="ConnectionError"),
    )
    result = finish(
        db, claim(db, bid), response(observed_at=observed - timedelta(days=1))
    )
    assert (
        result["status"] == "error" and result["error"] == "Source timestamp regressed"
    )
    assert assessment(db, m)[0]["hold"] is True


def test_lease_expiry_without_successor_is_not_accepted(db):
    b, m = binding(db)
    bid = b.binding_id
    acquired = claim(db, bid)
    row = db.get(ProviderBinding, bid)
    row.refresh_lease_expires_at = datetime.now(timezone.utc) - timedelta(seconds=1)
    db.commit()
    with pytest.raises(RefreshConflict):
        finish(db, acquired, response())
    db.refresh(row)
    assert row.refresh_token is None and row.refresh_status == "discarded"
    assert db.query(ProviderEvidence).filter_by(binding_id=bid).count() == 0


def test_identity_change_during_io_discards_response_and_releases_lease(db):
    b, m = binding(db)
    bid = b.binding_id
    mid = m.merchant_id
    acquired = claim(db, bid)
    with SessionLocal() as other:
        merchant = other.get(MerchantIdentity, mid)
        merchant.version += 1
        other.commit()
    with pytest.raises(RefreshConflict):
        finish(db, acquired, response())
    db.expire_all()
    assert db.get(ProviderBinding, bid).refresh_token is None


def test_operations_tracks_feed_failure_and_clears_condition_after_recovery(
    db, monkeypatch
):
    from app.core.operations import detect
    from app.models.db import Incident, AlertDelivery

    monkeypatch.setenv("SENTINEL_PROVIDER_REFRESH_ENABLED", "1")
    b, m = binding(db, auto_refresh=True)
    bid = b.binding_id
    detect(db)
    case = db.query(Incident).filter_by(kind="provider_evidence", entity_id=bid).one()
    finish(db, claim(db, bid), response())
    detect(db)
    db.refresh(case)
    assert case.state == "open" and not case.condition_active
    # Keep this fixture independent from the operational tests that follow it.
    db.query(AlertDelivery).filter_by(incident_id=case.incident_id).delete()
    db.delete(case)
    db.commit()


def test_scheduler_api_configuration_is_versioned_and_busy_requests_conflict(db):
    from fastapi.testclient import TestClient
    from app.api.main import app

    p = make_policy(db)
    m = identity(db, p)
    c = TestClient(app)
    headers = {"X-API-Key": "test-key-contracts"}
    body = dict(
        merchant_id=m.merchant_id,
        provider="recorded_future",
        external_id="idn:" + m.domain,
        expected_version=0,
        auto_refresh=True,
        refresh_interval_seconds=600,
    )
    saved = c.post("/providers/bindings", headers=headers, json=body)
    assert saved.status_code == 200
    bid = saved.json()["binding_id"]
    acquired = claim(db, bid)
    assert (
        c.post("/providers/bindings/" + bid + "/refresh", headers=headers).status_code
        == 409
    )
    assert (
        c.post("/providers/bindings/missing/refresh", headers=headers).status_code
        == 404
    )
    body.update(expected_version=1, auto_refresh=False)
    assert c.post("/providers/bindings", headers=headers, json=body).status_code == 200
    with pytest.raises(RefreshConflict):
        finish(db, acquired, response())
    listed = next(
        r
        for r in c.get("/providers", headers=headers).json()["bindings"]
        if r["binding_id"] == bid
    )
    assert (
        not listed["auto_refresh"]
        and not listed["refresh_in_progress"]
        and listed["version"] == 2
    )


def test_database_clock_caps_expiry_and_rejects_future_source_without_poisoning_watermark(
    db,
):
    b, m = binding(db)
    bid = b.binding_id
    bad = finish(
        db,
        claim(db, bid),
        response(observed_at=datetime.now(timezone.utc) + timedelta(hours=1)),
    )
    assert bad["status"] == "error"
    badrow = db.get(ProviderEvidence, bad["evidence_id"])
    assert badrow.observed_at is None
    good = finish(
        db,
        claim(db, bid),
        response(expires_at=datetime.now(timezone.utc) + timedelta(days=20)),
    )
    goodrow = db.get(ProviderEvidence, good["evidence_id"])
    assert good["status"] == "current"
    assert (goodrow.expires_at - goodrow.fetched_at).total_seconds() <= 3600


def test_scheduler_cannot_silently_reattest_a_riskrecon_mapping_after_identity_change(
    db, monkeypatch
):
    monkeypatch.setenv("SENTINEL_PROVIDER_REFRESH_ENABLED", "1")
    b, m = binding(
        db, provider="riskrecon", external_id="licensed-toe", auto_refresh=True
    )
    bid = b.binding_id
    m.legal_name = "Different legal entity"
    m.version += 1
    db.commit()
    with patch("app.core.providers.fetch") as external:
        assert run_one(db) is None
        external.assert_not_called()
    assert db.get(ProviderBinding, bid).refresh_status == "binding_changed"

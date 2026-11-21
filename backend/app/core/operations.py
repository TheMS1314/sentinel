"""Durable incident detection and at-least-once, signed operational notifications."""

import hashlib
import hmac
import json
import os
from datetime import timedelta
from urllib.parse import urlsplit
import uuid
import requests
from sqlalchemy import and_, or_, update
from app.core.intelligence import processing_time, control_event, utc
from app.core.locking import agent_transaction
from app.core.settings import integer
from app.models.db import (
    Incident,
    AlertDelivery,
    PaymentRecord,
    EvaluationJob,
    DecisionRecord,
    ProviderBinding,
    ProviderEvidence,
    MerchantIdentity,
)

MONITOR_LOCK = "__sentinel_operations_v1__"


def incident_view(row):
    return {
        k: getattr(row, k)
        for k in (
            "incident_id",
            "kind",
            "entity_id",
            "severity",
            "state",
            "condition_active",
            "summary",
            "version",
            "assigned_to",
            "last_reason",
        )
    } | {
        "first_seen": utc(row.first_seen).isoformat(),
        "last_seen": utc(row.last_seen).isoformat(),
    }


def open_incident(db, kind, entity, severity, summary):
    now = processing_time(db)
    fingerprint = hashlib.sha256((kind + ":" + entity).encode()).hexdigest()
    row = db.query(Incident).filter_by(fingerprint=fingerprint).first()
    notify = False
    if row is None:
        row = Incident(
            incident_id="incident_" + uuid.uuid4().hex,
            fingerprint=fingerprint,
            kind=kind,
            entity_id=entity,
            severity=severity,
            state="open",
            condition_active=True,
            summary=summary,
            version=1,
            first_seen=now,
            last_seen=now,
        )
        db.add(row)
        db.flush()
        notify = True
    elif row.state == "resolved" and kind == "failed_job":
        return row
    elif row.state == "resolved":
        before = incident_view(row)
        row.state, row.condition_active, row.summary = "open", True, summary
        row.version += 1
        row.last_seen = now
        control_event(
            db,
            "system-monitor",
            "incident_reopen",
            row.incident_id,
            before,
            incident_view(row),
        )
        notify = True
    else:
        row.condition_active, row.last_seen = True, now
    if notify:
        db.add(
            AlertDelivery(
                event_id="alert_" + uuid.uuid4().hex,
                incident_id=row.incident_id,
                payload={
                    "incident_id": row.incident_id,
                    "kind": kind,
                    "entity_id": entity,
                    "severity": severity,
                    "summary": "Manual operational review opened; inspect Sentinel"
                    if kind == "manual_review"
                    else summary,
                    "observed_at": now.isoformat(),
                },
                state="pending",
                attempts=0,
                next_attempt_at=now,
            )
        )
    return row


def provider_condition(db, binding, now):
    from app.core.provider_refresh import scheduler_enabled

    if not binding.auto_refresh:
        return None
    merchant = db.get(MerchantIdentity, binding.merchant_id)
    if merchant is None or not merchant.active:
        return None
    if binding.merchant_version != merchant.version:
        return "binding_changed"
    if not scheduler_enabled():
        return "worker_disabled"
    evidence = (
        db.query(ProviderEvidence)
        .filter_by(binding_id=binding.binding_id)
        .order_by(
            ProviderEvidence.refresh_generation.desc(),
            ProviderEvidence.fetched_at.desc(),
            ProviderEvidence.evidence_id.desc(),
        )
        .first()
    )
    if evidence is None:
        return "missing"
    if (
        evidence.binding_version != binding.version
        or evidence.merchant_version != merchant.version
    ):
        return "binding_changed"
    if evidence.status != "current":
        return evidence.status
    if utc(evidence.expires_at) <= now:
        return "stale"
    return None


def detect(db, quota_ok=True, provider_worker_ok=True):
    """Bound each scan; a shared transaction lock deduplicates replica detectors."""
    with agent_transaction(db, MONITOR_LOCK):
        candidates = []
        unknown_count = db.query(PaymentRecord).filter_by(state="unknown").count()
        if unknown_count > 200:
            candidates.append(
                (
                    "unknown_payment_backlog",
                    "payment-queue",
                    "critical",
                    "Unknown-payment backlog exceeds the individual scan bound; reconcile the entire backlog",
                )
            )
        for row in db.query(PaymentRecord).filter_by(state="unknown").limit(200):
            candidates.append(
                (
                    "unknown_payment",
                    row.request_id,
                    "critical",
                    "Payment outcome is unknown; retain reservation and reconcile",
                )
            )
        for row in (
            db.query(EvaluationJob)
            .filter_by(status="FAILED")
            .order_by(EvaluationJob.created_at.desc())
            .limit(100)
        ):
            candidates.append(
                (
                    "failed_job",
                    row.job_id,
                    "warning",
                    "Evaluation job failed; inspect its durable request before retrying",
                )
            )
        now = processing_time(db)
        for binding in (
            db.query(ProviderBinding)
            .filter_by(auto_refresh=True)
            .order_by(ProviderBinding.refresh_next_at, ProviderBinding.binding_id)
            .limit(200)
        ):
            condition = provider_condition(db, binding, now)
            if condition:
                candidates.append(
                    (
                        "provider_evidence",
                        binding.binding_id,
                        "warning",
                        "Scheduled provider evidence is "
                        + condition
                        + "; refresh or review the binding",
                    )
                )
        if not provider_worker_ok:
            candidates.append(
                (
                    "provider_worker",
                    "provider-refresh",
                    "warning",
                    "Provider refresh worker is stalled; cached evidence still expires and payment checks remain enforced",
                )
            )
        old = (
            db.query(EvaluationJob)
            .filter(
                EvaluationJob.status.in_(("QUEUED", "PROCESSING")),
                EvaluationJob.created_at
                < now
                - timedelta(
                    seconds=integer(
                        "SENTINEL_ALERT_QUEUE_AGE_SECONDS", 60, maximum=3600
                    )
                ),
            )
            .first()
        )
        if old:
            candidates.append(
                (
                    "queue_age",
                    "evaluation-queue",
                    "warning",
                    "Accepted work is older than the configured operational threshold",
                )
            )
        count = (
            db.query(DecisionRecord)
            .filter_by(verdict="HOLD", human_override=None)
            .count()
        )
        if count >= integer("SENTINEL_ALERT_REVIEW_BACKLOG", 50, maximum=100000):
            candidates.append(
                (
                    "review_backlog",
                    "review-queue",
                    "warning",
                    "Human-review backlog reached the configured threshold",
                )
            )
        if not quota_ok:
            candidates.append(
                (
                    "quota_dependency",
                    "redis",
                    "critical",
                    "Shared quota dependency is unavailable; HTTP admission fails closed",
                )
            )
        # Only clear a condition after checking the corresponding entity, not
        # because a bounded scan happened to exclude it.
        for incident in (
            db.query(Incident)
            .filter(Incident.condition_active.is_(True))
            .order_by(Incident.last_checked_at.asc().nullsfirst(), Incident.incident_id)
            .limit(300)
        ):
            incident.last_checked_at = now
            cleared = False
            if incident.kind == "unknown_payment":
                row = db.get(PaymentRecord, incident.entity_id)
                cleared = row is not None and row.state != "unknown"
            elif incident.kind == "unknown_payment_backlog":
                cleared = unknown_count <= 200
            elif incident.kind == "provider_evidence":
                binding = db.get(ProviderBinding, incident.entity_id)
                cleared = (
                    binding is None or provider_condition(db, binding, now) is None
                )
            elif incident.kind == "provider_worker":
                cleared = provider_worker_ok
            elif incident.kind == "quota_dependency":
                cleared = quota_ok
            elif incident.kind == "queue_age":
                cleared = old is None
            elif incident.kind == "review_backlog":
                cleared = count < integer(
                    "SENTINEL_ALERT_REVIEW_BACKLOG", 50, maximum=100000
                )
            if cleared:
                incident.condition_active = False
        for args in candidates:
            open_incident(db, *args)
    return len(candidates)


def endpoint():
    raw = os.environ.get("SENTINEL_ALERT_WEBHOOK_URL", "")
    if not raw:
        return None
    parsed = urlsplit(raw)
    allowed = {
        h.strip().lower()
        for h in os.environ.get("SENTINEL_ALERT_ALLOWED_HOSTS", "").split(",")
        if h.strip()
    }
    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or parsed.hostname.lower() not in allowed
        or parsed.username
        or parsed.password
        or parsed.fragment
    ):
        raise ValueError(
            "Alert delivery requires an HTTPS endpoint on the configured host allowlist"
        )
    secret = os.environ.get("SENTINEL_ALERT_SIGNING_SECRET", "")
    if len(secret) < 32:
        raise ValueError("Alert delivery requires a 32+ character signing secret")
    return raw, secret


def claim_delivery(db):
    with agent_transaction(db, MONITOR_LOCK):
        now = processing_time(db)
        for _ in range(16):
            row = (
                db.query(AlertDelivery)
                .filter(
                    or_(
                        and_(
                            AlertDelivery.state == "pending",
                            AlertDelivery.next_attempt_at <= now,
                        ),
                        and_(
                            AlertDelivery.state == "processing",
                            AlertDelivery.lease_expires_at < now,
                        ),
                    )
                )
                .order_by(AlertDelivery.next_attempt_at)
                .with_for_update(skip_locked=True)
                .first()
            )
            if row is None:
                return None
            if row.attempts >= 5:
                row.state, row.last_error = (
                    "failed",
                    "Retry budget exhausted; explicit administrator retry required",
                )
                db.flush()
                continue
            row.state, row.lease_token = "processing", uuid.uuid4().hex
            row.lease_expires_at = now + timedelta(seconds=30)
            row.attempts += 1
            return row.event_id, row.lease_token, dict(row.payload), row.attempts
        return None


def deliver_one(db):
    configured = endpoint()
    if configured is None:
        return False
    claimed = claim_delivery(db)
    if claimed is None:
        return False
    event_id, token, payload, attempt = claimed
    raw = json.dumps(
        dict(payload, event_id=event_id), sort_keys=True, separators=(",", ":")
    ).encode()
    url, secret = configured
    signature = hmac.new(secret.encode(), raw, hashlib.sha256).hexdigest()
    error = None
    try:
        with requests.Session() as session:
            session.trust_env = False
            with session.post(
                url,
                data=raw,
                headers={
                    "Content-Type": "application/json",
                    "X-Sentinel-Event-Id": event_id,
                    "X-Sentinel-Signature": signature,
                },
                timeout=(3, 5),
                allow_redirects=False,
                stream=True,
                verify=os.environ.get("SENTINEL_ALERT_CA_BUNDLE") or True,
            ) as response:
                if not 200 <= response.status_code < 300:
                    error = "http_status_" + str(response.status_code)
    except requests.RequestException as exc:
        error = type(exc).__name__
    now = processing_time(db)
    db.execute(
        update(AlertDelivery)
        .where(
            AlertDelivery.event_id == event_id,
            AlertDelivery.lease_token == token,
            AlertDelivery.state == "processing",
        )
        .values(
            state="pending"
            if error and attempt < 5
            else "failed"
            if error
            else "delivered",
            last_error=error,
            next_attempt_at=now + timedelta(seconds=min(300, 15 * 2 ** (attempt - 1))),
            delivered_at=None if error else now,
            lease_token=None,
            lease_expires_at=None,
        )
    )
    db.commit()
    return True

"""Local, explainable risk enrichment; no proprietary provider data is fabricated."""

from datetime import datetime, timedelta, timezone
import unicodedata
from sqlalchemy import func, text
from app.models.db import (
    MerchantRiskProfile,
    ThreatIndicator,
    TransactionRecord,
    PolicyRecord,
    DispatchGuard,
    DispatchClaim,
    RiskControlEvent,
)

GUARD_LOCK = "__sentinel_dispatch_guard_v1__"


def merchant_key(name):
    return " ".join(unicodedata.normalize("NFKC", name).casefold().split())


def utc(value):
    return (
        value.replace(tzinfo=timezone.utc)
        if value.tzinfo is None
        else value.astimezone(timezone.utc)
    )


def processing_time(db):
    if db.bind.dialect.name == "postgresql":
        return db.execute(text("SELECT clock_timestamp()")).scalar_one()
    return datetime.now(timezone.utc)


def guard_view(row):
    return {
        "version": row.version if row else 0,
        "frozen": row.frozen if row else False,
        "window_seconds": row.window_seconds if row else 60,
        "max_dispatches": row.max_dispatches if row else None,
        "max_amount_paise": row.max_amount_paise if row else None,
        "reason": row.reason if row else "No emergency limits configured",
        "updated_by": row.updated_by if row else None,
    }


def control_event(db, actor, kind, entity, before, after):
    db.add(
        RiskControlEvent(
            actor_id=actor, kind=kind, entity_id=entity, before=before, after=after
        )
    )


def assess(db, policy, request):
    """Additional signals can restrict authority, never expand it.

    Directory bindings are operator assertions, not authenticated merchant identity.
    Entity correlation is bounded to 501 recent owner-scoped events and descriptive.
    """
    now = processing_time(db)
    signals = []
    profile = (
        db.query(MerchantRiskProfile)
        .filter_by(
            owner_id=policy.owner_human_id,
            merchant_key=merchant_key(request.merchant_name),
        )
        .first()
    )
    vendor = {
        "status": "unknown",
        "score": None,
        "meaning": "cyber exposure heuristic, not fraud probability",
    }
    threat = {"status": "not_assessed", "matches": []}
    verdict = "PASS"
    if profile:
        fresh = utc(profile.expires_at) > now
        vendor.update(
            status="current" if fresh else "stale",
            score=profile.cyber_risk_score,
            source=profile.source,
            domain=profile.domain,
            expires_at=utc(profile.expires_at).isoformat(),
        )
        if not fresh:
            verdict = "HOLD"
            signals.append("Merchant risk evidence is stale; refresh or review it")
        elif profile.cyber_risk_score >= 70:
            verdict = "HOLD"
            signals.append(
                "Merchant cyber exposure score is at least 70/100 (uncalibrated)"
            )
        # Exact canonical domain only. No substring or subdomain assumptions.
        rows = (
            db.query(ThreatIndicator)
            .filter_by(domain=profile.domain, active=True)
            .all()
        )
        matches = []
        stale = 0
        for row in rows:
            if utc(row.expires_at) <= now:
                stale += 1
                continue
            matches.append(
                {
                    "indicator_id": row.indicator_id,
                    "source": row.source,
                    "confirmed": row.confirmed,
                    "evidence": row.evidence,
                    "expires_at": utc(row.expires_at).isoformat(),
                }
            )
            if (
                row.confirmed
                and row.source == "operator"
                and profile.source == "operator"
            ):
                verdict = "BLOCK"
                signals.append(
                    "Operator-confirmed threat indicator matches the configured merchant domain"
                )
            else:
                if verdict != "BLOCK":
                    verdict = "HOLD"
                signals.append(
                    "Unconfirmed or synthetic threat indicator matches; human review required"
                )
        if stale and verdict == "PASS":
            verdict = "HOLD"
            signals.append(
                "Threat evidence for this domain has expired; refresh or review it"
            )
        threat = {
            "status": "matched" if matches else "stale" if stale else "no_local_match",
            "matches": matches,
            "expired_matches": stale,
            "note": "Local operator/synthetic records only; absence is not evidence of safety",
        }
    # The window follows trusted processing time, not caller-provided timestamps.
    owner_agents = (
        db.query(PolicyRecord.agent_id)
        .filter_by(owner_human_id=policy.owner_human_id)
        .distinct()
    )
    rows = (
        (
            db.query(TransactionRecord)
            .filter(
                TransactionRecord.agent_id.in_(owner_agents),
                TransactionRecord.created_at >= now - timedelta(minutes=5),
                TransactionRecord.created_at <= now,
                TransactionRecord.request_id != request.request_id,
            )
            .order_by(TransactionRecord.id.desc())
            .limit(501)
            .all()
        )
        if profile is not None
        else []
    )
    truncated = len(rows) > 500
    matching = [
        r
        for r in rows[:500]
        if merchant_key(r.merchant_name) == merchant_key(request.merchant_name)
    ]
    agents = {r.agent_id for r in matching} | {request.agent_id}
    correlated = profile is not None and len(matching) >= 4 and len(agents) >= 3
    if correlated:
        if verdict != "BLOCK":
            verdict = "HOLD"
        signals.append(
            "Several agents in this owner's account are targeting the same merchant within five minutes"
        )
    return {
        "version": "sentinel-local-intelligence-v1",
        "mode": "local_evidence",
        "assessed_at": now.isoformat(),
        "recommendation": verdict,
        "coverage": "partial" if profile else "merchant_identity_unmapped",
        "signals": signals,
        "vendor": vendor,
        "threat": threat,
        "relations": {
            "enabled": profile is not None,
            "window_seconds": 300,
            "prior_matching_attempts": len(matching),
            "agents_including_current": len(agents),
            "truncated": truncated,
            "alert": correlated,
            "calibrated": False,
        },
        "providers": {
            "mastercard_decision_intelligence": "not_connected",
            "recorded_future": "not_connected",
            "riskrecon": "not_connected",
        },
    }


def dispatch_permission(db, payment):
    """Call under GUARD_LOCK and commit claim and payment transition together.

    A freeze prevents new claims. Calls already claimed can still finish.
    Dispatch counts include failed/unknown attempts, so failures cannot evade limits.
    """
    row = db.query(DispatchGuard).filter_by(id=1).populate_existing().first()
    if row is None:
        return None  # Compatibility: no network-wide limits configured yet.
    if row.frozen:
        return "Payment dispatch is frozen: " + row.reason
    now = processing_time(db)
    count, total = (
        db.query(
            func.count(DispatchClaim.request_id),
            func.coalesce(func.sum(DispatchClaim.amount_paise), 0),
        )
        .filter(
            DispatchClaim.claimed_at >= now - timedelta(seconds=row.window_seconds),
        )
        .one()
    )
    if row.max_dispatches is not None and count >= row.max_dispatches:
        return "Emergency rolling dispatch-count limit reached"
    if (
        row.max_amount_paise is not None
        and total + payment.amount_paise > row.max_amount_paise
    ):
        return "Emergency rolling dispatch-amount limit reached"
    return None


def confirmed_dispatch_threat(db, request_id):
    """Recheck new confirmed evidence just before claiming an approved outbox row."""
    from app.models.db import DecisionRecord

    row = (
        db.query(TransactionRecord, DecisionRecord.policy_snapshot)
        .join(DecisionRecord, DecisionRecord.request_id == TransactionRecord.request_id)
        .filter(TransactionRecord.request_id == request_id)
        .first()
    )
    if not row or not row[1]:
        return None
    profile = (
        db.query(MerchantRiskProfile)
        .filter_by(
            owner_id=row[1]["owner_human_id"],
            merchant_key=merchant_key(row[0].merchant_name),
            source="operator",
        )
        .first()
    )
    if (
        profile
        and db.query(ThreatIndicator)
        .filter(
            ThreatIndicator.domain == profile.domain,
            ThreatIndicator.source == "operator",
            ThreatIndicator.confirmed.is_(True),
            ThreatIndicator.active.is_(True),
            ThreatIndicator.expires_at > processing_time(db),
        )
        .first()
    ):
        return "Current confirmed threat evidence prevents payment dispatch"
    return None


def dispatch_authority(db, request_id):
    """An authorization cannot outlive revoked credentials or a replaced policy."""
    import os
    from app.core.auth import (
        principal_for_subject,
        database_authority,
        principal_from_row,
    )
    from app.models.db import DecisionRecord, PaymentRecord, ServiceCredential

    tx, decision = (
        db.query(TransactionRecord, DecisionRecord)
        .join(DecisionRecord, DecisionRecord.request_id == TransactionRecord.request_id)
        .filter(TransactionRecord.request_id == request_id)
        .one()
    )
    actor = (
        decision.resolved_by
        if decision.human_override == "APPROVE"
        else tx.submitted_by
    )
    if (
        actor in ("internal", "legacy")
        and os.environ.get("SENTINEL_ENV") != "production"
    ):
        return None
    # All mutation paths lock credential authority before policy records.
    # This makes revocation linearizable with claims without a reverse lock order.
    if database_authority():
        row = (
            db.query(ServiceCredential)
            .filter_by(subject=actor)
            .populate_existing()
            .with_for_update()
            .first()
        )
        principal = principal_from_row(row)
    else:
        principal = principal_for_subject(actor)
    payment = db.get(PaymentRecord, request_id)
    if (
        principal is None
        or principal.credential_version != payment.authorizing_credential_version
    ):
        return "Authorizing credential was revoked or rotated before dispatch"
    current = (
        db.query(PolicyRecord)
        .filter_by(agent_id=tx.agent_id, active=True)
        .populate_existing()
        .with_for_update()
        .first()
    )
    bound_policy = payment.authorization_policy_id or decision.policy_id
    if current is None or current.policy_id != bound_policy:
        return "Owner policy changed after approval; cancel and submit a fresh request"
    roles = (
        ("admin", "owner", "reviewer")
        if decision.human_override == "APPROVE"
        else ("admin", "owner", "agent")
    )
    if principal.role not in roles or not principal.permits_agent(tx.agent_id, db):
        return "Authorizing credential no longer permits this agent"
    return None

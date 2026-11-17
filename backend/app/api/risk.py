"""Audited administrator controls and scoped decision evidence."""

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session
from app.core.auth import (
    Principal,
    require_api_key,
    require_agent,
    assert_current_principal,
)
from app.core.rate_limit import enforce_rate_limit
from app.core.intelligence_schemas import MerchantProfileIn, IndicatorIn, GuardIn
from app.core.intelligence import (
    merchant_key,
    guard_view,
    control_event,
    GUARD_LOCK,
    processing_time,
    utc,
)
from app.core.locking import agent_transaction
from app.core.orchestrator import ConflictError
from app.models.db import (
    get_session,
    MerchantRiskProfile,
    ThreatIndicator,
    DispatchGuard,
    RiskControlEvent,
    IntelligenceAssessment,
    TransactionRecord,
    PaymentRecord,
)

router = APIRouter(
    prefix="/risk", dependencies=[Depends(require_api_key), Depends(enforce_rate_limit)]
)


def evidence_view(row):
    result = {c.name: getattr(row, c.name) for c in row.__table__.columns}
    for name in ("expires_at", "updated_at"):
        if result.get(name):
            result[name] = utc(result[name]).isoformat()
    return result


@router.get("/overview")
def overview(
    db: Session = Depends(get_session), principal: Principal = Depends(require_api_key)
):
    principal.require("admin")
    now = processing_time(db)
    from app.core.providers import configuration

    return {
        "mode": "local_evidence",
        "providers": {
            "mastercard_decision_intelligence": "not_connected",
            **configuration(),
        },
        "guard": guard_view(db.get(DispatchGuard, 1)),
        "pending_payments": db.query(PaymentRecord).filter_by(state="pending").count(),
        "pending": [
            {
                "request_id": p.request_id,
                "amount_paise": p.amount_paise,
                "expires_at": utc(p.dispatch_expires_at).isoformat()
                if p.dispatch_expires_at
                else None,
                "reason": p.error,
            }
            for p in db.query(PaymentRecord)
            .filter_by(state="pending")
            .order_by(PaymentRecord.updated_at)
            .limit(20)
        ],
        "current_indicators": db.query(ThreatIndicator)
        .filter(ThreatIndicator.active.is_(True), ThreatIndicator.expires_at > now)
        .count(),
        "merchant_profiles": db.query(MerchantRiskProfile).count(),
        "events": [
            {
                "id": e.id,
                "actor": e.actor_id,
                "kind": e.kind,
                "entity_id": e.entity_id,
                "created_at": utc(e.created_at).isoformat(),
            }
            for e in db.query(RiskControlEvent)
            .order_by(RiskControlEvent.id.desc())
            .limit(20)
        ],
        "note": "Independent dispatch guard; existing claims may finish after a freeze. Scores are uncalibrated; vendor identity is operator asserted.",
    }


@router.post("/guard")
def set_guard(
    body: GuardIn,
    db: Session = Depends(get_session),
    principal: Principal = Depends(require_api_key),
):
    principal.require("admin")
    with agent_transaction(db, GUARD_LOCK):
        assert_current_principal(principal, db)
        row = db.query(DispatchGuard).filter_by(id=1).populate_existing().first()
        before = guard_view(row)
        if body.expected_version != before["version"]:
            raise ConflictError("Guard configuration changed; refresh before saving")
        if row is None:
            row = DispatchGuard(id=1)
            db.add(row)
        for name, value in body.model_dump(exclude={"expected_version"}).items():
            setattr(row, name, value)
        row.version = before["version"] + 1
        row.updated_by, row.updated_at = principal.subject, processing_time(db)
        after = guard_view(row)
        control_event(db, principal.subject, "dispatch_guard", "global", before, after)
    return after


@router.get("/merchants")
def merchants(
    db: Session = Depends(get_session), principal: Principal = Depends(require_api_key)
):
    principal.require("admin")
    return [
        evidence_view(r)
        for r in db.query(MerchantRiskProfile)
        .order_by(MerchantRiskProfile.id.desc())
        .limit(200)
    ]


@router.post("/merchants")
def save_merchant(
    body: MerchantProfileIn,
    db: Session = Depends(get_session),
    principal: Principal = Depends(require_api_key),
):
    principal.require("admin")
    key = merchant_key(body.merchant_name)
    with agent_transaction(db, GUARD_LOCK):
        assert_current_principal(principal, db)
        row = (
            db.query(MerchantRiskProfile)
            .filter_by(owner_id=body.owner_id, merchant_key=key)
            .first()
        )
        before = evidence_view(row) if row else None
        if row is None:
            row = MerchantRiskProfile(owner_id=body.owner_id, merchant_key=key)
            db.add(row)
        for name, value in body.model_dump().items():
            setattr(row, name, value)
        row.updated_by, row.updated_at = principal.subject, processing_time(db)
        db.flush()
        after = evidence_view(row)
        control_event(
            db, principal.subject, "merchant_profile", str(row.id), before, after
        )
    return after


@router.get("/indicators")
def indicators(
    db: Session = Depends(get_session), principal: Principal = Depends(require_api_key)
):
    principal.require("admin")
    return [
        evidence_view(r)
        for r in db.query(ThreatIndicator)
        .order_by(ThreatIndicator.updated_at.desc())
        .limit(200)
    ]


@router.post("/indicators")
def save_indicator(
    body: IndicatorIn,
    db: Session = Depends(get_session),
    principal: Principal = Depends(require_api_key),
):
    principal.require("admin")
    with agent_transaction(db, GUARD_LOCK):
        assert_current_principal(principal, db)
        row = db.get(ThreatIndicator, body.indicator_id)
        before = evidence_view(row) if row else None
        if row is None:
            row = ThreatIndicator(indicator_id=body.indicator_id)
            db.add(row)
        for name, value in body.model_dump().items():
            setattr(row, name, value)
        row.updated_by, row.updated_at = principal.subject, processing_time(db)
        after = evidence_view(row)
        control_event(
            db, principal.subject, "threat_indicator", row.indicator_id, before, after
        )
    return after


@router.get("/assessments/{request_id}")
def assessment(
    request_id: str,
    db: Session = Depends(get_session),
    principal: Principal = Depends(require_api_key),
):
    tx = db.query(TransactionRecord).filter_by(request_id=request_id).first()
    if tx is None:
        raise HTTPException(404, "Unknown request")
    require_agent(principal, tx.agent_id, db)
    row = db.get(IntelligenceAssessment, request_id)
    if row is None:
        return {
            "status": "not_assessed",
            "note": "Legacy or hard-blocked transaction; no intelligence snapshot",
        }
    return row.report

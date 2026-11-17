"""Administrator incident handling and protected low-cardinality metrics."""

from fastapi import APIRouter, Depends, HTTPException, Response
from pydantic import Field
from typing import Literal
from sqlalchemy import func
from sqlalchemy.orm import Session
from app.core.auth import (
    Principal,
    require_api_key,
    assert_current_principal,
    principal_for_subject,
)
from app.core.rate_limit import enforce_rate_limit
from app.core.schemas import StrictModel, ID
from app.core.operations import MONITOR_LOCK, incident_view, open_incident, endpoint
from app.core.intelligence import control_event, processing_time, utc
from app.core.locking import agent_transaction
from app.models.db import (
    get_session,
    Incident,
    AlertDelivery,
    PaymentRecord,
    EvaluationJob,
    TransactionRecord,
)

router = APIRouter(
    prefix="/ops", dependencies=[Depends(require_api_key), Depends(enforce_rate_limit)]
)


class IncidentUpdate(StrictModel):
    expected_version: int = Field(ge=1)
    state: Literal["acknowledged", "resolved"]
    reason: str = Field(min_length=1, max_length=2000)
    assigned_to: ID | None = None


class ManualIncident(StrictModel):
    request_id: ID
    summary: str = Field(min_length=1, max_length=2000)


class AlertRetry(StrictModel):
    reason: str = Field(min_length=1, max_length=2000)


@router.get("/summary")
def summary(
    db: Session = Depends(get_session), principal: Principal = Depends(require_api_key)
):
    principal.require("admin")
    return {
        "delivery": "configured" if endpoint() else "not_configured",
        "incidents": dict(
            db.query(Incident.state, func.count()).group_by(Incident.state)
        ),
        "alerts": dict(
            db.query(AlertDelivery.state, func.count()).group_by(AlertDelivery.state)
        ),
    }


@router.get("/incidents")
def incidents(
    state: Literal["open", "acknowledged", "resolved"] | None = None,
    db: Session = Depends(get_session),
    principal: Principal = Depends(require_api_key),
):
    principal.require("admin")
    query = db.query(Incident)
    if state:
        query = query.filter_by(state=state)
    return [
        incident_view(r) for r in query.order_by(Incident.last_seen.desc()).limit(100)
    ]


@router.post("/incidents")
def manual(
    body: ManualIncident,
    db: Session = Depends(get_session),
    principal: Principal = Depends(require_api_key),
):
    principal.require("admin")
    with agent_transaction(db, MONITOR_LOCK):
        assert_current_principal(principal, db)
        if (
            db.query(TransactionRecord).filter_by(request_id=body.request_id).first()
            is None
        ):
            raise HTTPException(404, "Unknown request")
        row = open_incident(
            db, "manual_review", body.request_id, "warning", body.summary
        )
        control_event(
            db,
            principal.subject,
            "incident_manual",
            row.incident_id,
            None,
            incident_view(row),
        )
        result = incident_view(row)
    return result


@router.post("/incidents/{incident_id}")
def update_incident(
    incident_id: str,
    body: IncidentUpdate,
    db: Session = Depends(get_session),
    principal: Principal = Depends(require_api_key),
):
    principal.require("admin")
    with agent_transaction(db, MONITOR_LOCK):
        assert_current_principal(principal, db)
        row = (
            db.query(Incident)
            .filter_by(incident_id=incident_id)
            .populate_existing()
            .with_for_update()
            .first()
        )
        if row is None:
            raise HTTPException(404, "Unknown incident")
        if row.version != body.expected_version:
            raise HTTPException(409, "Incident changed; refresh before saving")
        if row.state == "resolved":
            raise HTTPException(409, "Incident is already resolved")
        if (
            body.state == "resolved"
            and row.condition_active
            and row.kind not in ("failed_job", "manual_review")
        ):
            raise HTTPException(409, "The underlying condition is still active")
        if body.assigned_to:
            assignee = principal_for_subject(body.assigned_to, db)
            if assignee is None or assignee.role != "admin":
                raise HTTPException(422, "Assign incidents to an active administrator")
        before = incident_view(row)
        row.state, row.assigned_to, row.last_reason = (
            body.state,
            body.assigned_to,
            body.reason,
        )
        if row.state == "resolved":
            row.condition_active = False
        row.version += 1
        result = incident_view(row)
        control_event(
            db,
            principal.subject,
            "incident_" + body.state,
            row.incident_id,
            before,
            result,
        )
    return result


@router.get("/alerts")
def alerts(
    db: Session = Depends(get_session), principal: Principal = Depends(require_api_key)
):
    principal.require("admin")
    return [
        {
            "event_id": r.event_id,
            "incident_id": r.incident_id,
            "state": r.state,
            "attempts": r.attempts,
            "last_error": r.last_error,
            "created_at": utc(r.created_at).isoformat(),
        }
        for r in db.query(AlertDelivery)
        .order_by(AlertDelivery.created_at.desc())
        .limit(100)
    ]


@router.post("/alerts/{event_id}/retry")
def retry(
    event_id: str,
    body: AlertRetry,
    db: Session = Depends(get_session),
    principal: Principal = Depends(require_api_key),
):
    principal.require("admin")
    with agent_transaction(db, MONITOR_LOCK):
        assert_current_principal(principal, db)
        row = (
            db.query(AlertDelivery)
            .filter_by(event_id=event_id)
            .populate_existing()
            .with_for_update()
            .first()
        )
        if row is None:
            raise HTTPException(404, "Unknown alert")
        if row.state != "failed":
            raise HTTPException(409, "Only terminal failed deliveries can be retried")
        row.state, row.attempts, row.next_attempt_at = "pending", 0, processing_time(db)
        row.lease_token, row.lease_expires_at = None, None
        control_event(
            db,
            principal.subject,
            "alert_retry",
            event_id,
            {"state": "failed"},
            {"state": "pending", "reason": body.reason},
        )
    return {"state": "pending", "event_id": event_id}


@router.get("/metrics")
def prometheus(
    db: Session = Depends(get_session), principal: Principal = Depends(require_api_key)
):
    principal.require("admin")
    lines = []
    for name, model, column, known in (
        (
            "sentinel_payments",
            PaymentRecord,
            PaymentRecord.state,
            (
                "pending",
                "processing",
                "unknown",
                "created",
                "mock",
                "captured",
                "failed",
                "expired",
                "cancelled",
            ),
        ),
        (
            "sentinel_jobs",
            EvaluationJob,
            EvaluationJob.status,
            ("QUEUED", "PROCESSING", "DONE", "FAILED"),
        ),
        (
            "sentinel_incidents",
            Incident,
            Incident.state,
            ("open", "acknowledged", "resolved"),
        ),
        (
            "sentinel_alert_deliveries",
            AlertDelivery,
            AlertDelivery.state,
            ("pending", "processing", "delivered", "failed"),
        ),
    ):
        counts = dict(db.query(column, func.count()).group_by(column))
        lines.append("# TYPE " + name + " gauge")
        lines.extend(
            f'{name}{{state="{state}"}} {counts.get(state, 0)}' for state in known
        )
    oldest = (
        db.query(func.min(EvaluationJob.created_at))
        .filter(EvaluationJob.status.in_(("QUEUED", "PROCESSING")))
        .scalar()
    )
    age = max(0, (processing_time(db) - utc(oldest)).total_seconds()) if oldest else 0
    lines.extend(
        [
            "# TYPE sentinel_oldest_job_age_seconds gauge",
            "sentinel_oldest_job_age_seconds " + str(age),
        ]
    )
    return Response("\n".join(lines) + "\n", media_type="text/plain; version=0.0.4")

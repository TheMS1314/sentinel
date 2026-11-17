"""Conservative recovery: a process crash is not proof an order was never created."""

from datetime import timedelta
from app.core.intelligence import processing_time
from app.core.settings import integer
from app.models.db import PaymentRecord, DecisionRecord


def recover_stale_dispatches(db, limit=50):
    cutoff = processing_time(db) - timedelta(
        seconds=integer(
            "SENTINEL_DISPATCH_STALE_SECONDS", 120, minimum=30, maximum=3600
        )
    )
    rows = (
        db.query(PaymentRecord)
        .filter(
            PaymentRecord.state == "processing",
            PaymentRecord.updated_at < cutoff,
        )
        .order_by(PaymentRecord.updated_at)
        .with_for_update(skip_locked=True)
        .limit(limit)
        .all()
    )
    for row in rows:
        row.state = "unknown"
        row.error = "Dispatch worker stopped before a known outcome; provider reconciliation required"
        decision = db.query(DecisionRecord).filter_by(request_id=row.request_id).first()
        if decision:
            decision.razorpay_status = "unknown"
    db.commit()
    return len(rows)

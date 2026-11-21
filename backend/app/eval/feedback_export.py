"""Export candidates only: an override is not an independently adjudicated label."""

from app.core.intelligence import utc
from app.models.db import IntelligenceAssessment
from app.models.policy import TransactionRequest


def export_case(db, decision):
    tx = decision.transaction
    evidence = db.get(IntelligenceAssessment, decision.request_id)
    snapshot = evidence.report if evidence else {}
    return dict(
        case_id="review_" + decision.request_id,
        category="human_feedback",
        policy=decision.policy_snapshot,
        request={
            k: float(tx.amount) if k == "amount" else getattr(tx, k)
            for k in TransactionRequest.__dataclass_fields__
        },
        source_created_at=utc(tx.created_at).isoformat() if tx.created_at else None,
        source_resolved_at=utc(decision.resolved_at).isoformat()
        if decision.resolved_at
        else None,
        history=[],
        policy_overrides={},
        expected_verdict=decision.human_override,
        original_verdict=decision.verdict,
        reviewer=decision.resolved_by,
        notes=decision.resolution_reason,
        is_judgment_call=True,
        requires_history_review=True,
        identity_snapshot=snapshot.get("identity"),
        intelligence_snapshot=snapshot,
    )

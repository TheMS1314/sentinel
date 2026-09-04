"""
Sentinel Audit Logger
======================
The single write-path for recording what happened. Every transaction
request and every decision Sentinel makes goes through here -- this is
what makes the system's behavior reconstructable after the fact, which
is the whole point of an "audit trail" for a payments trust layer.

Nothing else in the codebase should write to the transactions/decisions
tables directly -- always go through AuditLogger so the log stays
complete and consistent.
"""

from __future__ import annotations
from sqlalchemy.orm import Session

from app.models.db import TransactionRecord, DecisionRecord
from app.models.policy import TransactionRequest, Decision, HistoryItem


class AuditLogger:
    def __init__(self, db: Session):
        self.db = db

    def log_transaction(self, request: TransactionRequest, scenario_tag: str = "") -> TransactionRecord:
        record = TransactionRecord(
            request_id=request.request_id,
            agent_id=request.agent_id,
            merchant_name=request.merchant_name,
            merchant_category=request.merchant_category,
            amount=request.amount,
            requested_at_hour=request.requested_at_hour,
            requested_at_iso=request.requested_at_iso,
            stated_intent=request.stated_intent,
            raw_agent_goal=request.raw_agent_goal,
            scenario_tag=scenario_tag,
        )
        self.db.add(record)
        self.db.commit()
        self.db.refresh(record)
        return record

    def log_decision(self, decision: Decision) -> DecisionRecord:
        record = DecisionRecord(
            request_id=decision.request_id,
            verdict=decision.verdict,
            reasons=decision.reasons,
            checked_rules=decision.checked_rules,
            decided_by=decision.decided_by,
            similar_past_cases=decision.similar_past_cases,
            confidence=decision.confidence,
            model_version=decision.model_version,
        )
        self.db.add(record)
        self.db.commit()
        self.db.refresh(record)
        return record

    def update_razorpay_status(self, request_id: str, status: str, ref: str | None = None):
        record = self.db.query(DecisionRecord).filter_by(request_id=request_id).first()
        if record:
            record.razorpay_status = status
            record.razorpay_ref = ref
            self.db.commit()

    def resolve_hold(self, request_id: str, human_verdict: str):
        """Human resolves a HOLD (HITL) -- APPROVE or BLOCK, recorded distinctly
        from the original verdict so we never lose the fact that it was held."""
        record = self.db.query(DecisionRecord).filter_by(request_id=request_id).first()
        if record:
            record.human_override = human_verdict
            self.db.commit()

    def get_recent_transactions_for_agent(self, agent_id: str, limit: int = 200) -> list[HistoryItem]:
        rows = (
            self.db.query(TransactionRecord)
            .filter_by(agent_id=agent_id)
            .order_by(TransactionRecord.requested_at_iso.desc())
            .limit(limit)
            .all()
        )
        items = []
        for r in rows:
            decision = self.db.query(DecisionRecord).filter_by(request_id=r.request_id).first()
            request = TransactionRequest(
                request_id=r.request_id,
                agent_id=r.agent_id,
                merchant_name=r.merchant_name,
                merchant_category=r.merchant_category,
                amount=r.amount,
                requested_at_hour=r.requested_at_hour,
                requested_at_iso=r.requested_at_iso,
                stated_intent=r.stated_intent,
                raw_agent_goal=r.raw_agent_goal,
            )
            items.append(HistoryItem(
                request=request,
                verdict=decision.verdict if decision else "UNKNOWN",
                human_override=decision.human_override if decision else None,
            ))
        return items

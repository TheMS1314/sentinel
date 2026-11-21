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
from sqlalchemy.orm import Session, joinedload
from sqlalchemy import func, or_, and_, case
from datetime import timedelta
from app.core.validation import utc_timestamp
from app.core.intelligence import processing_time

from app.models.db import TransactionRecord, DecisionRecord, PaymentRecord
from app.models.policy import TransactionRequest, Decision, HistoryItem


class AuditLogger:
    def __init__(self, db: Session):
        self.db = db

    def log_transaction(
        self,
        request: TransactionRequest,
        scenario_tag: str = "",
        actor_id: str = "internal",
        actor_version: int = 1,
    ) -> TransactionRecord:
        record = TransactionRecord(
            request_id=request.request_id,
            agent_id=request.agent_id,
            merchant_name=request.merchant_name,
            merchant_category=request.merchant_category,
            merchant_id=request.merchant_id,
            merchant_version=request.merchant_version,
            amount=request.amount,
            requested_at_hour=request.requested_at_hour,
            requested_at_iso=request.requested_at_iso,
            stated_intent=request.stated_intent,
            raw_agent_goal=request.raw_agent_goal,
            scenario_tag=scenario_tag,
            submitted_by=actor_id,
            submitted_credential_version=actor_version,
            created_at=processing_time(self.db),
        )
        self.db.add(record)
        self.db.flush()
        self.db.refresh(record)
        return record

    def log_decision(self, decision: Decision, policy=None) -> DecisionRecord:
        record = DecisionRecord(
            request_id=decision.request_id,
            verdict=decision.verdict,
            reasons=decision.reasons,
            checked_rules=decision.checked_rules,
            decided_by=decision.decided_by,
            similar_past_cases=decision.similar_past_cases,
            confidence=decision.confidence,
            model_version=decision.model_version,
            policy_id=decision.policy_id,
            policy_version=decision.policy_version,
            policy_snapshot=policy.to_dict() if policy else None,
            risk_score=decision.risk_score,
            prompt_version=decision.prompt_version,
            prompt_hash=decision.prompt_hash,
            llm_mode=decision.llm_mode,
            token_usage=decision.token_usage,
            created_at=processing_time(self.db),
        )
        self.db.add(record)
        self.db.flush()
        self.db.refresh(record)
        return record

    def update_razorpay_status(
        self, request_id: str, status: str, ref: str | None = None
    ):
        record = self.db.query(DecisionRecord).filter_by(request_id=request_id).first()
        if record:
            record.razorpay_status = status
            record.razorpay_ref = ref
            self.db.flush()

    def resolve_hold(self, request_id: str, human_verdict: str):
        """Human resolves a HOLD (HITL) -- APPROVE or BLOCK, recorded distinctly
        from the original verdict so we never lose the fact that it was held."""
        record = self.db.query(DecisionRecord).filter_by(request_id=request_id).first()
        if record:
            record.human_override = human_verdict
            self.db.flush()

    def get_recent_transactions_for_agent(
        self, agent_id: str, limit=200, before=None, approved_only=False
    ) -> list[HistoryItem]:
        # Full relevant history is required for budget/velocity correctness.
        # Eager load decisions and ledger state to avoid per-row lookups.
        rows = (
            self.db.query(
                TransactionRecord, PaymentRecord.state, PaymentRecord.budget_day
            )
            .outerjoin(
                PaymentRecord, PaymentRecord.request_id == TransactionRecord.request_id
            )
            .options(joinedload(TransactionRecord.decision))
            .filter(TransactionRecord.agent_id == agent_id)
        )
        if approved_only:
            rows = rows.filter(PaymentRecord.state.in_(["created", "mock", "captured"]))
        if before:
            rows = rows.filter(TransactionRecord.requested_at_iso <= before)
        rows = (
            rows.order_by(TransactionRecord.requested_at_iso.desc()).limit(limit).all()
        )
        items = []
        for r, state, budget_day in rows:
            decision = r.decision
            request = TransactionRequest(
                **{
                    key: float(r.amount) if key == "amount" else getattr(r, key)
                    for key in TransactionRequest.__dataclass_fields__
                }
            )
            items.append(
                HistoryItem(
                    request=request,
                    verdict=decision.verdict if decision else "UNKNOWN",
                    human_override=decision.human_override if decision else None,
                    budget_day=budget_day,
                    payment_status=state or ("not_sent" if decision else "unknown"),
                )
            )
        return items

    def budget_totals(self, agent_id, day):
        committed, captured = (
            self.db.query(
                func.coalesce(func.sum(PaymentRecord.amount_paise), 0),
                func.coalesce(
                    func.sum(
                        case(
                            (
                                PaymentRecord.state == "captured",
                                PaymentRecord.amount_paise,
                            ),
                            else_=0,
                        )
                    ),
                    0,
                ),
            )
            .join(
                TransactionRecord,
                TransactionRecord.request_id == PaymentRecord.request_id,
            )
            .filter(
                TransactionRecord.agent_id == agent_id,
                PaymentRecord.state.in_(
                    ["pending", "processing", "unknown", "created", "mock", "captured"]
                ),
                or_(
                    PaymentRecord.budget_day == day,
                    and_(
                        PaymentRecord.budget_day.is_(None),
                        TransactionRecord.requested_at_iso.startswith(day),
                    ),
                ),
            )
            .one()
        )
        return int(committed), int(captured)

    def evaluation_context(self, request, policy):
        timestamp = utc_timestamp(request.requested_at_iso)
        committed, _ = self.budget_totals(
            request.agent_id, timestamp.date().isoformat()
        )
        count = (
            self.db.query(func.count(TransactionRecord.id))
            .filter(
                TransactionRecord.agent_id == request.agent_id,
                TransactionRecord.requested_at_iso
                >= (
                    timestamp - timedelta(minutes=policy.velocity.window_minutes)
                ).isoformat(),
                TransactionRecord.requested_at_iso <= timestamp.isoformat(),
            )
            .scalar()
        )
        history = self.get_recent_transactions_for_agent(
            request.agent_id,
            limit=200,
            before=timestamp.isoformat(),
            approved_only=True,
        )
        return history, committed, int(count)

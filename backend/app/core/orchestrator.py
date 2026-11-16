"""Transactional authorization, durable payment intent and guarded human review."""
from dataclasses import asdict, fields
from datetime import datetime, timezone
import hashlib
import json
from sqlalchemy.orm import Session, joinedload
from app.models.policy import AgentPolicy, TransactionRequest, Decision
from app.models.db import DecisionRecord, TransactionRecord, PolicyRecord, PaymentRecord, ReviewEvent
from app.core.rule_engine import RuleEngine, ESCALATE, result_to_decision
from app.core.llm_reasoner import LLMReasoner
from app.core.audit_logger import AuditLogger
from app.core.razorpay_gateway import RazorpayGateway
from app.core.behavioral_baseline import BehavioralBaseline
from app.core.risk_score import RiskScorer
from app.core.validation import validate_request, validate_policy, paise, utc_timestamp
from app.core.locking import agent_transaction
from app.rag.case_retriever import CaseRetriever, PastCase


class ConflictError(ValueError):
    pass


def decision_from_record(record):
    keys = {f.name for f in fields(Decision)}
    return Decision(**{key: getattr(record, key) for key in keys})


class SentinelOrchestrator:
    def __init__(self, db: Session):
        self.db = db
        self.audit = AuditLogger(db)
        self.rule_engine = RuleEngine()
        self.retriever = CaseRetriever()
        self.reasoner = LLMReasoner(case_retriever=self.retriever)
        self.gateway = RazorpayGateway()
        self.behavioral_baseline = BehavioralBaseline()
        self.risk_scorer = RiskScorer(self.behavioral_baseline)

    def _refresh_case_index(self, agent_id, policy, request):
        # Rebuild a bounded index with durable policy/goal scoping. No cross-replica
        # invalidation race. Human corrections replace the original precedent.
        import os
        if os.environ.get('SENTINEL_DISABLE_RETRIEVAL') == '1':
            self.retriever.refresh([])
            return
        rows = (self.db.query(DecisionRecord).join(DecisionRecord.transaction)
                .options(joinedload(DecisionRecord.transaction))
                .filter(TransactionRecord.agent_id == agent_id,
                        TransactionRecord.raw_agent_goal == request.raw_agent_goal,
                        DecisionRecord.policy_id == policy.policy_id,
                        TransactionRecord.requested_at_iso <= request.requested_at_iso)
                .order_by(DecisionRecord.id.desc()).limit(500).all())
        self.retriever.refresh([PastCase(d.request_id,
            CaseRetriever._case_text(d.transaction.merchant_category, d.transaction.merchant_name,
                                     d.transaction.stated_intent, float(d.transaction.amount)),
            d.human_override or d.verdict,
            [d.resolution_reason] if d.human_override and d.resolution_reason else d.reasons,
            d.transaction.merchant_category, d.transaction.merchant_name, float(d.transaction.amount)) for d in rows])

    def evaluate_transaction(self, policy: AgentPolicy, request: TransactionRequest,
                             scenario_tag='', *, actor_id='internal', execute_payment=True) -> Decision:
        validate_request(request)
        if request.agent_id != policy.agent_id:
            raise ValueError('request and policy agent must match')
        validate_policy(policy)
        with agent_transaction(self.db, request.agent_id):
            current = (self.db.query(PolicyRecord).filter_by(agent_id=request.agent_id, active=True)
                       .order_by(PolicyRecord.version.desc()).first())
            if current:
                policy = AgentPolicy.from_dict(current.policy_json)
            existing = self.db.query(TransactionRecord).filter_by(request_id=request.request_id).first()
            if existing:
                actual = {k: float(existing.amount) if k == 'amount' else getattr(existing, k)
                          for k in TransactionRequest.__dataclass_fields__}
                if actual != asdict(request) or not existing.decision:
                    raise ConflictError('request_id is already bound to different or incomplete data')
                decision = decision_from_record(existing.decision)
            else:
                if actor_id != "internal" or not request.raw_agent_goal.strip():
                    request.raw_agent_goal = policy.authorized_goal
                history = [item for item in self.audit.get_recent_transactions_for_agent(request.agent_id)
                           if utc_timestamp(item.request.requested_at_iso) <= utc_timestamp(request.requested_at_iso)]
                self.audit.log_transaction(request, scenario_tag)
                result = self.rule_engine.evaluate(policy, request, history)
                if result.outcome == ESCALATE:
                    self._refresh_case_index(request.agent_id, policy, request)
                    decision = self.reasoner.evaluate(policy, request)
                else:
                    decision = result_to_decision(request, result)
                if decision.verdict == 'APPROVE':
                    score = self.risk_scorer.evaluate(history, request)
                    decision.risk_score = score.total
                    if score.is_high_risk:
                        decision.verdict = 'HOLD'
                        decision.reasons.append(RiskScorer.format_reason(score))
                        decision.checked_rules.append('behavioral_baseline')
                        decision.decided_by = 'behavioral_baseline'
                decision.policy_id, decision.policy_version = policy.policy_id, policy.version
                self.audit.log_decision(decision, policy)
                if decision.verdict == 'APPROVE' and execute_payment:
                    self.db.add(PaymentRecord(request_id=request.request_id,
                                              amount_paise=paise(request.amount), state='pending', budget_day=utc_timestamp(request.requested_at_iso).date().isoformat()))
        if decision.verdict == 'APPROVE' and execute_payment:
            self.dispatch_payment(request.request_id)
        return decision

    def dispatch_payment(self, request_id):
        # A durable pending row is an outbox entry. Claim before external IO.
        # Unknown outcomes must be reconciled, NEVER blindly resubmitted.
        payment = self.db.query(PaymentRecord).filter_by(request_id=request_id).with_for_update().first()
        if not payment:
            self.db.rollback()
            return {'status': 'not_sent'}
        if payment.state != 'pending':
            result = {'status': payment.state, 'order_id': payment.order_id}
            self.db.commit()
            return result
        payment.state = 'processing'
        amount = payment.amount_paise / 100
        self.db.commit()
        try:
            result = self.gateway.create_order(amount, request_id, {'request_id': request_id})
        except Exception:
            result = {'status': 'unknown', 'order_id': None}
        status = result.get('status', 'unknown')
        if status not in ('mock', 'created', 'failed', 'unknown'):
            status = 'unknown'
        payment = self.db.query(PaymentRecord).filter_by(request_id=request_id).with_for_update().one()
        payment.state, payment.order_id = status, result.get('order_id')
        payment.error = result.get('error')
        self.audit.update_razorpay_status(request_id, status, payment.order_id)
        self.db.commit()
        return {'status': status, 'order_id': payment.order_id}

    def resolve_hold(self, request_id, human_verdict, *, reviewer_id='internal', reason='Human review'):
        if human_verdict not in ('APPROVE', 'BLOCK'):
            raise ValueError('human_verdict must be APPROVE or BLOCK')
        tx = self.db.query(TransactionRecord).filter_by(request_id=request_id).first()
        if not tx:
            raise LookupError('unknown request_id')
        with agent_transaction(self.db, tx.agent_id):
            record = self.db.query(DecisionRecord).filter_by(request_id=request_id).populate_existing().one()
            if record.verdict != 'HOLD' or record.human_override is not None:
                raise ConflictError('only an unresolved HOLD can be resolved')
            if human_verdict == 'APPROVE':
                p = (self.db.query(PolicyRecord).filter_by(agent_id=tx.agent_id, active=True)
                     .order_by(PolicyRecord.version.desc()).first())
                policy = AgentPolicy.from_dict(p.policy_json if p else record.policy_snapshot)
                request = TransactionRequest(**{k: float(tx.amount) if k == 'amount' else getattr(tx, k)
                                                for k in TransactionRequest.__dataclass_fields__})
                # At review time current UTC clock controls budget/day/time in production.
                import os
                if os.environ.get('SENTINEL_ENV') == 'production':
                    now = datetime.now(timezone.utc)
                    request.requested_at_hour, request.requested_at_iso = now.hour, now.isoformat()
                history = [i for i in self.audit.get_recent_transactions_for_agent(tx.agent_id)
                           if i.request.request_id != request_id]
                check = self.rule_engine.evaluate(policy, request, history)
                if check.outcome == 'BLOCK':
                    raise ConflictError('Current policy rejects approval: ' + '; '.join(check.reasons))
                self.db.add(PaymentRecord(request_id=request_id, amount_paise=paise(tx.amount), state='pending', budget_day=utc_timestamp(request.requested_at_iso).date().isoformat()))
            record.human_override = human_verdict
            record.resolved_by, record.resolved_at = reviewer_id, datetime.now(timezone.utc)
            record.resolution_reason = reason
            self.db.add(ReviewEvent(request_id=request_id, reviewer_id=reviewer_id,
                                    verdict=human_verdict, reason=reason))
        return self.dispatch_payment(request_id) if human_verdict == 'APPROVE' else {'status': 'blocked_by_human'}

"""
Sentinel Orchestrator
======================
The single entry point that ties everything together:

    TransactionRequest
        -> log to audit trail
        -> RuleEngine (deterministic, primary)
            -> APPROVE / BLOCK / HOLD  -> final, done
            -> ESCALATE                -> LLMReasoner (secondary)
                -> APPROVE / BLOCK / HOLD -> final
        -> log Decision to audit trail
        -> if APPROVE: attempt via RazorpayGateway
        -> if HOLD: surfaced to a human for confirmation (HITL) --
           nothing happens until resolve_hold() is called
        -> if BLOCK: nothing happens, ever, for this request

This is the module the API layer and the dashboard both call into --
neither of them should talk to the rule engine, reasoner, or gateway
directly.
"""

from __future__ import annotations
import logging
from sqlalchemy.orm import Session

from app.models.policy import AgentPolicy, TransactionRequest, Decision, VERDICT_APPROVE, VERDICT_HOLD, VERDICT_BLOCK
from app.core.rule_engine import RuleEngine, ESCALATE, result_to_decision
from app.core.llm_reasoner import LLMReasoner
from app.core.audit_logger import AuditLogger
from app.core.razorpay_gateway import RazorpayGateway
from app.rag.case_retriever import CaseRetriever, PastCase
from app.models.db import DecisionRecord
from app.core.cache import case_index_cache
from app.core.behavioral_baseline import BehavioralBaseline
from app.core.risk_score import RiskScorer

logger = logging.getLogger("sentinel.orchestrator")


class SentinelOrchestrator:
    def __init__(self, db: Session):
        self.db = db
        self.audit = AuditLogger(db)
        self.rule_engine = RuleEngine()
        self.retriever = CaseRetriever()
        self.reasoner = LLMReasoner(case_retriever=self.retriever)
        self.gateway = RazorpayGateway()
        self.behavioral_baseline = BehavioralBaseline()
        self.risk_scorer = RiskScorer(behavioral_baseline=self.behavioral_baseline)

    def _refresh_case_index(self, agent_id: str):
        """Rebuild the RAG index from this agent's audit history before
        reasoning on an escalated case. Cached briefly (see cache.py) --
        escalations for the same agent often cluster in time (a burst of
        ambiguous requests from one adversarial run, for example), so
        refitting TF-IDF from scratch on every single one is wasted work.
        The cache is invalidated the moment a new decision is logged for
        this agent, so it never serves stale precedent."""
        cache_key = f"case_index:{agent_id}"
        cached = case_index_cache.get(cache_key)
        if cached is not None:
            self.retriever._cases = cached["cases"]
            self.retriever._vectorizer = cached["vectorizer"]
            self.retriever._matrix = cached["matrix"]
            return

        rows = (
            self.db.query(DecisionRecord)
            .join(DecisionRecord.transaction)
            .filter(DecisionRecord.transaction.has(agent_id=agent_id))
            .all()
        )
        cases = []
        for d in rows:
            tx = d.transaction
            if tx is None:
                continue
            cases.append(
                PastCase(
                    request_id=d.request_id,
                    text=CaseRetriever._case_text(tx.merchant_category, tx.merchant_name, tx.stated_intent, tx.amount),
                    verdict=d.verdict,
                    reasons=d.reasons or [],
                    merchant_category=tx.merchant_category,
                    merchant_name=tx.merchant_name,
                    amount=tx.amount,
                )
            )
        self.retriever.refresh(cases)
        case_index_cache.set(cache_key, {
            "cases": self.retriever._cases,
            "vectorizer": self.retriever._vectorizer,
            "matrix": self.retriever._matrix,
        })

    def evaluate_transaction(
        self,
        policy: AgentPolicy,
        request: TransactionRequest,
        scenario_tag: str = "",
    ) -> Decision:
        # 1. Fetch history BEFORE logging this request -- a transaction must
        #    only ever be evaluated against what genuinely happened before
        #    it. Logging first and then querying "recent transactions" would
        #    incorrectly include this very request in its own history,
        #    double-counting it in the velocity check (found via a live
        #    burst-load test: a 3-transaction limit was tripping after only
        #    2 real prior transactions because each request was seeing
        #    itself in the count).
        history = self.audit.get_recent_transactions_for_agent(request.agent_id)

        # 2. Now log the request itself -- always, regardless of what happens next.
        self.audit.log_transaction(request, scenario_tag=scenario_tag)

        # 3. Deterministic rule engine, primary decision layer.
        rule_result = self.rule_engine.evaluate(policy, request, history)

        if rule_result.outcome != ESCALATE:
            decision = result_to_decision(request, rule_result)
        else:
            # 3. Only ambiguous cases reach the LLM reasoner, grounded by
            #    retrieval over this agent's own past decisions.
            self._refresh_case_index(request.agent_id)
            decision = self.reasoner.evaluate(policy, request)

        # 3b. Composite risk scoring -- a secondary check, independent of
        #     the static rules above. Only runs on requests about to be
        #     approved: a transaction that already failed a hard rule or
        #     is already held doesn't need a second reason to not proceed.
        #     This can downgrade APPROVE -> HOLD (never upgrade to BLOCK --
        #     a risk score is a "let a human look at this" signal, not
        #     proof of a violation).
        #
        #     Combines four weak signals (amount deviation, timing
        #     deviation, merchant familiarity, category familiarity) into
        #     one weighted score -- see risk_score.py. This is a strict
        #     upgrade over a single binary anomaly flag: it can catch a
        #     request where no ONE signal alone is unusual enough to flag,
        #     but several mild signals together are, which is closer to
        #     how production fraud-scoring engines actually work.
        if decision.verdict == VERDICT_APPROVE:
            score = self.risk_scorer.evaluate(history, request)
            if score.is_high_risk:
                decision = Decision(
                    request_id=decision.request_id,
                    verdict=VERDICT_HOLD,
                    reasons=decision.reasons + [RiskScorer.format_reason(score)],
                    checked_rules=decision.checked_rules + ["behavioral_baseline"],
                    decided_by="behavioral_baseline",
                    similar_past_cases=decision.similar_past_cases,
                    confidence=score.total / score.max_total,
                )

        # 4. Log the decision -- the audit trail is complete regardless of verdict.
        self.audit.log_decision(decision)
        case_index_cache.invalidate_prefix(f"case_index:{request.agent_id}")

        # 5. Only APPROVE ever reaches Razorpay. HOLD waits for a human.
        #    BLOCK never proceeds, full stop.
        if decision.verdict == VERDICT_APPROVE:
            result = self.gateway.create_order(
                amount_inr=request.amount,
                request_id=request.request_id,
                notes={"agent_id": request.agent_id, "merchant": request.merchant_name},
            )
            self.audit.update_razorpay_status(request.request_id, result["status"], result.get("order_id"))
        elif decision.verdict == VERDICT_HOLD:
            logger.info(f"HITL: request {request.request_id} held for human confirmation.")
        else:
            logger.info(f"BLOCKED: request {request.request_id} -- {decision.reasons}")

        return decision

    def resolve_hold(self, request_id: str, human_verdict: str) -> dict:
        """A human resolves a HOLD -- this is the HITL completion step.
        Only APPROVE or BLOCK are valid human resolutions."""
        if human_verdict not in (VERDICT_APPROVE, VERDICT_BLOCK):
            raise ValueError("human_verdict must be APPROVE or BLOCK")

        self.audit.resolve_hold(request_id, human_verdict)

        if human_verdict == VERDICT_APPROVE:
            record = self.db.query(DecisionRecord).filter_by(request_id=request_id).first()
            tx = record.transaction if record else None
            if tx:
                result = self.gateway.create_order(
                    amount_inr=tx.amount,
                    request_id=request_id,
                    notes={"agent_id": tx.agent_id, "merchant": tx.merchant_name, "human_approved": "true"},
                )
                self.audit.update_razorpay_status(request_id, result["status"], result.get("order_id"))
                return result

        return {"status": "blocked_by_human"}

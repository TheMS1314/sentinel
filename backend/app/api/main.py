"""
Sentinel API
=============
FastAPI app exposing the orchestrator, policy management, transaction
feed, HITL resolution, agent simulation triggers, and eval reports to
the React dashboard.
"""

from __future__ import annotations
import os
import logging
from datetime import timezone

from app.core.env import load_env
load_env()  # must run before any app import below reads os.environ
from fastapi import FastAPI, Depends, HTTPException, Request, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from fastapi.exceptions import RequestValidationError
from sqlalchemy.orm import Session, joinedload
from sqlalchemy.exc import SQLAlchemyError
from pydantic import BaseModel, Field
from typing import Optional
import uuid

logger = logging.getLogger("sentinel.api")

from app.models.db import init_db, get_session, TransactionRecord, DecisionRecord, PolicyRecord
from app.models.policy import AgentPolicy, TransactionRequest, TimeWindow, VelocityLimit
from app.core.orchestrator import SentinelOrchestrator
from app.core.job_queue import EvaluationQueue
from app.core.redis_queue import make_queue
from app.core.cache import policy_cache
from app.core.auth import require_api_key
from app.core.rate_limit import enforce_rate_limit
from app.core.logging_middleware import StructuredLoggingMiddleware
from app.models.db import SessionLocal
from app.agents.purchasing_agent import PurchasingAgent, AdversarialDriftType

app = FastAPI(title="Sentinel API")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # dev-only; tighten before any real deployment
    allow_methods=["*"],
    allow_headers=["*"],
)
app.add_middleware(StructuredLoggingMiddleware)


# ---------- Global error handling ----------
# Every error path returns the same envelope shape ({"error": {...}}) so
# the frontend never has to special-case FastAPI's default vs. a custom
# HTTPException vs. an unhandled exception. Unhandled exceptions are
# logged with a traceback server-side but never leak that traceback to
# the client -- a stack trace in an API response is an information
# disclosure bug, not just an ugly one.

@app.exception_handler(HTTPException)
async def http_exception_handler(request: Request, exc: HTTPException):
    return JSONResponse(
        status_code=exc.status_code,
        content={"error": {"message": exc.detail, "status_code": exc.status_code}},
    )


@app.exception_handler(RequestValidationError)
async def validation_exception_handler(request: Request, exc: RequestValidationError):
    return JSONResponse(
        status_code=422,
        content={"error": {"message": "Invalid request", "status_code": 422, "details": exc.errors()}},
    )


@app.exception_handler(SQLAlchemyError)
async def db_exception_handler(request: Request, exc: SQLAlchemyError):
    logger.exception(f"Unhandled database error on {request.method} {request.url.path}")
    return JSONResponse(
        status_code=503,
        content={"error": {"message": "Database temporarily unavailable", "status_code": 503}},
    )


@app.exception_handler(Exception)
async def unhandled_exception_handler(request: Request, exc: Exception):
    logger.exception(f"Unhandled error on {request.method} {request.url.path}")
    return JSONResponse(
        status_code=500,
        content={"error": {"message": "Internal server error", "status_code": 500}},
    )


from fastapi import APIRouter, Depends as _Depends

# Every route except /health requires the API key. Using an included
# router (rather than a global dependency on `app`) keeps /health
# publicly reachable for uptime checks / load balancer health probes,
# which themselves shouldn't need a credential.
secured = APIRouter(dependencies=[_Depends(require_api_key), _Depends(enforce_rate_limit)])


def _process_single_evaluation(payload: dict) -> dict:
    """The actual synchronous evaluation work, run inside a worker thread
    by the EvaluationQueue. Opens its own DB session since sessions are
    not thread-safe to share across the queue's worker pool."""
    db = SessionLocal()
    try:
        agent_id = payload["agent_id"]
        policy_record = (
            db.query(PolicyRecord)
            .filter_by(agent_id=agent_id, active=True)
            .order_by(PolicyRecord.version.desc())
            .first()
        )
        if not policy_record:
            raise ValueError(f"No active policy for agent {agent_id}")
        policy = AgentPolicy.from_dict(policy_record.policy_json)

        payload.setdefault("request_id", f"req_{uuid.uuid4().hex[:10]}")
        request = TransactionRequest(**{k: v for k, v in payload.items() if k != "scenario_tag"})

        orchestrator = SentinelOrchestrator(db)
        decision = orchestrator.evaluate_transaction(policy, request, scenario_tag=payload.get("scenario_tag", ""))
        db.commit()
        return decision.__dict__
    finally:
        db.close()


eval_queue = make_queue(submit_fn=_process_single_evaluation, num_workers=4)


@app.on_event("startup")
async def on_startup():
    await eval_queue.start()


@app.on_event("shutdown")
async def on_shutdown():
    await eval_queue.stop()


# ---------- Schemas ----------

class PolicyIn(BaseModel):
    agent_id: str
    owner_human_id: str
    max_amount_per_txn: float = 2000.0
    max_daily_total: float = 5000.0
    allowed_categories: list[str] = ["grocery"]
    blocked_merchants: list[str] = []
    allowed_merchants: Optional[list[str]] = None
    time_window_start: int = 6
    time_window_end: int = 22
    velocity_max_count: int = 3
    velocity_window_minutes: int = 1440


class HoldResolutionIn(BaseModel):
    human_verdict: str  # APPROVE or BLOCK


class SimulateIn(BaseModel):
    agent_id: str
    goal: str = "buy weekly groceries under budget"
    mode: str = "well_behaved"  # or "adversarial"
    drift_type: AdversarialDriftType = "none"
    # Bounded to match the frontend's own min=1/max=10 hint on the
    # number input -- those HTML attributes don't actually stop invalid
    # JS state from being sent (a typed "9999" bypasses them entirely),
    # so the real boundary has to be here. An unbounded value previously
    # let a client force PurchasingAgent to synchronously generate and
    # evaluate an arbitrarily large batch in a single /simulate request.
    num_transactions: int = Field(default=5, ge=1, le=10)


# ---------- Policy endpoints ----------

@secured.post("/policies")
def create_policy(policy_in: PolicyIn, db: Session = Depends(get_session)):
    # Deactivate any existing active policies for this agent and compute
    # the next version -- without this, two policies for the same agent
    # could both end up active=True with the same version, making
    # "get latest" genuinely ambiguous (found via a live multi-replica
    # test: an update through one backend replica wasn't reliably visible
    # from another, which looked like a cache bug but was actually this).
    existing = db.query(PolicyRecord).filter_by(agent_id=policy_in.agent_id, active=True).all()
    next_version = 1
    for row in existing:
        row.active = False
        next_version = max(next_version, row.version + 1)

    policy_id = f"pol_{uuid.uuid4().hex[:10]}"
    policy = AgentPolicy(
        policy_id=policy_id,
        agent_id=policy_in.agent_id,
        owner_human_id=policy_in.owner_human_id,
        max_amount_per_txn=policy_in.max_amount_per_txn,
        max_daily_total=policy_in.max_daily_total,
        allowed_categories=policy_in.allowed_categories,
        blocked_merchants=policy_in.blocked_merchants,
        allowed_merchants=policy_in.allowed_merchants,
        time_window=TimeWindow(policy_in.time_window_start, policy_in.time_window_end),
        velocity=VelocityLimit(policy_in.velocity_max_count, policy_in.velocity_window_minutes),
        version=next_version,
    )
    record = PolicyRecord(
        policy_id=policy_id,
        agent_id=policy_in.agent_id,
        owner_human_id=policy_in.owner_human_id,
        version=next_version,
        active=True,
        policy_json=policy.to_dict(),
    )
    db.add(record)
    db.commit()
    policy_cache.invalidate(f"policy:{policy_in.agent_id}")  # a new policy supersedes any cached one
    return {"policy_id": policy_id, "policy": policy.to_dict()}


@secured.get("/policies/{agent_id}")
def get_latest_policy(agent_id: str, db: Session = Depends(get_session)):
    cache_key = f"policy:{agent_id}"
    cached = policy_cache.get(cache_key)
    if cached is not None:
        return cached

    record = (
        db.query(PolicyRecord)
        .filter_by(agent_id=agent_id, active=True)
        .order_by(PolicyRecord.version.desc())
        .first()
    )
    if not record:
        raise HTTPException(404, "No active policy for this agent")
    result = {"policy_id": record.policy_id, "policy": record.policy_json, "version": record.version}
    policy_cache.set(cache_key, result)
    return result


@secured.get("/policies")
def list_policies(db: Session = Depends(get_session)):
    records = db.query(PolicyRecord).filter_by(active=True).all()
    return [{"policy_id": r.policy_id, "agent_id": r.agent_id, "policy": r.policy_json} for r in records]


# ---------- Transaction feed ----------

def _serialize_transaction(tx: TransactionRecord) -> dict:
    decision = tx.decision  # populated via joinedload -- no extra query per row
    return {
        "request_id": tx.request_id,
        "agent_id": tx.agent_id,
        "merchant_name": tx.merchant_name,
        "merchant_category": tx.merchant_category,
        "amount": tx.amount,
        "requested_at_iso": tx.requested_at_iso,
        "stated_intent": tx.stated_intent,
        "scenario_tag": tx.scenario_tag,
        "verdict": decision.verdict if decision else None,
        "reasons": decision.reasons if decision else [],
        "decided_by": decision.decided_by if decision else None,
        "human_override": decision.human_override if decision else None,
        "razorpay_status": decision.razorpay_status if decision else None,
        "confidence": decision.confidence if decision else None,
        "model_version": decision.model_version if decision else None,
        "similar_past_cases": decision.similar_past_cases if decision else [],
        # Section 26 fix: requested_at_iso is the (often deterministic,
        # scenario-generated) event time the agent claims to have made
        # the request at -- fine for reproducible eval data, but
        # showing ONLY that in the UI means running the demo on a day
        # other than the scenario's reference date makes every
        # transaction look like it happened over a week ago, with no
        # explanation. created_at is the actual DB persist time --
        # i.e. when Sentinel really processed it -- and was already
        # being stored but never exposed via the API. Both are now
        # available so the frontend can show REQUESTED vs PROCESSED
        # distinctly instead of one ambiguous timestamp.
        "created_at_iso": tx.created_at.replace(tzinfo=timezone.utc).isoformat() if tx.created_at else None,
    }


@secured.get("/transactions")
def list_transactions(
    limit: Optional[int] = None,
    page: Optional[int] = Query(default=None, ge=1),
    page_size: int = Query(default=25, ge=1, le=200),
    agent_id: Optional[str] = None,
    verdict: Optional[str] = None,
    search: Optional[str] = None,
    scenario_tag: Optional[str] = None,
    db: Session = Depends(get_session),
):
    """Two modes, chosen by which params are passed:

    - `limit` (legacy): returns a bare list, most-recent-first, capped at
      `limit`. Kept so the pre-pagination frontend build doesn't break.
    - `page`/`page_size`: returns {"items": [...], "page", "page_size",
      "total", "total_pages"} -- the real paginated contract.

    Both modes share one query builder (with filters) and one joinedload
    to avoid the N+1 (a separate decision lookup per transaction) the
    original implementation had.
    """
    query = db.query(TransactionRecord).options(joinedload(TransactionRecord.decision))

    # Golden-dataset evaluation runs persist their transactions (needed
    # so velocity-dependent golden cases have real history to score
    # against -- see eval_harness.py), but into the SAME table the live
    # feed reads from. Without this exclusion, running the eval harness
    # floods the Live Feed with 100+ golden_eval/golden_history rows
    # that have nothing to do with the actual demo -- confirmed by
    # running the harness against a clean seed: transaction count went
    # from 18 to 153. The Live Feed must only ever show real demo/
    # production transactions, never evaluation artifacts.
    # NULL-safe: `NOT (NULL LIKE 'golden_%')` evaluates to NULL (not
    # TRUE) in SQL's three-valued logic, so a bare `~col.startswith(...)`
    # would silently drop every row with no scenario_tag at all -- the
    # explicit OR keeps those rows.
    query = query.filter(
        (TransactionRecord.scenario_tag.is_(None)) | (~TransactionRecord.scenario_tag.startswith("golden_"))
    )

    if agent_id:
        query = query.filter(TransactionRecord.agent_id == agent_id)
    if scenario_tag:
        query = query.filter(TransactionRecord.scenario_tag == scenario_tag)
    if search:
        like = f"%{search}%"
        query = query.filter(
            (TransactionRecord.merchant_name.ilike(like))
            | (TransactionRecord.agent_id.ilike(like))
            | (TransactionRecord.stated_intent.ilike(like))
        )
    if verdict:
        query = query.join(DecisionRecord, DecisionRecord.request_id == TransactionRecord.request_id).filter(
            DecisionRecord.verdict == verdict.upper()
        )

    query = query.order_by(TransactionRecord.created_at.desc())

    if page is not None:
        page = max(page, 1)
        total = query.count()
        rows = query.offset((page - 1) * page_size).limit(page_size).all()
        return {
            "items": [_serialize_transaction(tx) for tx in rows],
            "page": page,
            "page_size": page_size,
            "total": total,
            "total_pages": max(1, (total + page_size - 1) // page_size),
        }

    rows = query.limit(limit or 100).all()
    return [_serialize_transaction(tx) for tx in rows]


@secured.get("/transactions/scenarios")
def list_scenario_tags(db: Session = Depends(get_session)):
    """Distinct scenario tags actually present, for building filter UI --
    without shipping every transaction row to the client just to
    dedupe a small set of tags client-side."""
    rows = (
        db.query(TransactionRecord.scenario_tag)
        .filter(TransactionRecord.scenario_tag.isnot(None), TransactionRecord.scenario_tag != "")
        .filter(~TransactionRecord.scenario_tag.startswith("golden_"))  # never NULL here, filtered above
        .distinct()
        .all()
    )
    return sorted(r[0] for r in rows)


@secured.get("/transactions/summary")
def transactions_summary(agent_id: Optional[str] = None, db: Session = Depends(get_session)):
    from sqlalchemy import func

    # Same golden_* exclusion as /transactions -- the summary card
    # shown next to the Live Feed must count only real demo
    # transactions, not evaluation-harness noise. NULL-safe for the
    # same reason as the /transactions filter above. Also scoped to
    # agent_id when provided, so the stats bar stays consistent with
    # whichever agent the Live Feed is currently filtered to -- an
    # unscoped summary next to an agent-scoped feed would show
    # mismatched totals in a multi-agent dashboard.
    query = (
        db.query(DecisionRecord.verdict, func.count(DecisionRecord.id))
        .join(TransactionRecord, TransactionRecord.request_id == DecisionRecord.request_id)
        .filter(
            (TransactionRecord.scenario_tag.is_(None)) | (~TransactionRecord.scenario_tag.startswith("golden_"))
        )
    )
    if agent_id:
        query = query.filter(TransactionRecord.agent_id == agent_id)
    rows = query.group_by(DecisionRecord.verdict).all()
    by_verdict = {"APPROVE": 0, "HOLD": 0, "BLOCK": 0}
    total = 0
    for verdict, count in rows:
        if verdict in by_verdict:
            by_verdict[verdict] = count
        total += count
    return {"total": total, "by_verdict": by_verdict}


# ---------- Evaluate + HITL ----------

@secured.post("/transactions/evaluate")
def evaluate_transaction(request_in: dict, db: Session = Depends(get_session)):
    agent_id = request_in["agent_id"]
    policy_record = (
        db.query(PolicyRecord)
        .filter_by(agent_id=agent_id, active=True)
        .order_by(PolicyRecord.version.desc())
        .first()
    )
    if not policy_record:
        raise HTTPException(404, "No active policy for this agent")
    policy = AgentPolicy.from_dict(policy_record.policy_json)

    request_in.setdefault("request_id", f"req_{uuid.uuid4().hex[:10]}")
    request = TransactionRequest(**request_in)

    orchestrator = SentinelOrchestrator(db)
    decision = orchestrator.evaluate_transaction(policy, request, scenario_tag=request_in.get("scenario_tag", ""))
    return decision.__dict__


@secured.post("/holds/{request_id}/resolve")
def resolve_hold(request_id: str, body: HoldResolutionIn, db: Session = Depends(get_session)):
    orchestrator = SentinelOrchestrator(db)
    try:
        result = orchestrator.resolve_hold(request_id, body.human_verdict)
    except ValueError as e:
        raise HTTPException(400, str(e))
    return result


# ---------- Async batch evaluation (queue) ----------
# Demonstrates real concurrent throughput: a burst of transaction requests
# is enqueued immediately (the caller doesn't block on evaluation), and a
# small worker pool processes them concurrently. Poll /jobs/{job_id} for
# each result, or /jobs/batch/{batch_id} for the whole batch's status.

class BatchEvaluateIn(BaseModel):
    requests: list[dict]  # each dict matches the /transactions/evaluate body shape


@secured.post("/transactions/evaluate-batch")
async def evaluate_batch(body: BatchEvaluateIn):
    if not body.requests:
        raise HTTPException(400, "requests must contain at least one item")
    batch_id = f"batch_{uuid.uuid4().hex[:10]}"
    job_ids = []
    for req in body.requests:
        job_id = await eval_queue.enqueue(req, batch_id=batch_id)
        job_ids.append(job_id)
    return {"batch_id": batch_id, "job_ids": job_ids, "queued": len(job_ids)}


@secured.get("/jobs/{job_id}")
def get_job_status(job_id: str):
    job = eval_queue.get_job(job_id)
    if job is None:
        raise HTTPException(404, "Unknown job_id")
    # Normalize across backends: the in-memory queue returns a Job
    # dataclass (status is a JobStatus enum), the Redis queue returns a
    # plain dict (status is already a string) -- both are valid, this
    # endpoint's job is just to present them identically to the client.
    if isinstance(job, dict):
        return job
    return {"job_id": job.job_id, "status": job.status.value, "result": job.result, "error": job.error}


@secured.get("/jobs/batch/{batch_id}")
def get_batch_status(batch_id: str):
    batch = eval_queue.get_batch(batch_id)
    if batch is None:
        raise HTTPException(404, "Unknown batch_id")
    return batch


@secured.get("/jobs")
def queue_stats():
    return eval_queue.stats()


# ---------- Simulation trigger ----------

@secured.post("/simulate")
def simulate(body: SimulateIn, db: Session = Depends(get_session)):
    policy_record = (
        db.query(PolicyRecord)
        .filter_by(agent_id=body.agent_id, active=True)
        .order_by(PolicyRecord.version.desc())
        .first()
    )
    if not policy_record:
        raise HTTPException(404, "No active policy for this agent -- create one first")
    policy = AgentPolicy.from_dict(policy_record.policy_json)

    agent = PurchasingAgent()
    requests = agent.run(
        agent_id=body.agent_id,
        goal=body.goal,
        mode=body.mode,
        drift_type=body.drift_type,
        num_transactions=body.num_transactions,
    )

    orchestrator = SentinelOrchestrator(db)
    scenario_tag = body.mode if body.mode == "well_behaved" else f"adversarial:{body.drift_type}"
    decisions = []
    for req in requests:
        decision = orchestrator.evaluate_transaction(policy, req, scenario_tag=scenario_tag)
        decisions.append({"request": req.__dict__, "decision": decision.__dict__})

    return {"scenario_tag": scenario_tag, "results": decisions}


@secured.post("/simulate-async")
async def simulate_async(body: SimulateIn, db: Session = Depends(get_session)):
    """Same as /simulate, but generates the agent's transaction requests
    and enqueues each through the real async job queue instead of
    evaluating them synchronously -- this is what actually exercises
    the concurrent worker pool, visible to the dashboard as each job
    moves QUEUED -> PROCESSING -> DONE."""
    policy_record = (
        db.query(PolicyRecord)
        .filter_by(agent_id=body.agent_id, active=True)
        .order_by(PolicyRecord.version.desc())
        .first()
    )
    if not policy_record:
        raise HTTPException(404, "No active policy for this agent -- create one first")

    agent = PurchasingAgent()
    requests = agent.run(
        agent_id=body.agent_id,
        goal=body.goal,
        mode=body.mode,
        drift_type=body.drift_type,
        num_transactions=body.num_transactions,
    )
    scenario_tag = body.mode if body.mode == "well_behaved" else f"adversarial:{body.drift_type}"

    batch_id = f"batch_{uuid.uuid4().hex[:10]}"
    job_ids = []
    for req in requests:
        payload = dict(req.__dict__)
        payload["scenario_tag"] = scenario_tag
        job_id = await eval_queue.enqueue(payload, batch_id=batch_id)
        job_ids.append(job_id)

    return {"scenario_tag": scenario_tag, "batch_id": batch_id, "job_ids": job_ids}


# ---------- Eval report ----------

@secured.get("/eval/report")
def get_eval_report():
    import json
    import os
    path = os.path.join(os.path.dirname(__file__), "..", "..", "data", "eval_report.json")
    path = os.path.abspath(path)
    if not os.path.exists(path):
        raise HTTPException(404, "No eval report yet -- run `python -m app.eval.eval_harness` first")
    with open(path) as f:
        report = json.load(f)

    hard_path = os.path.join(os.path.dirname(__file__), "..", "..", "data", "hard_eval_report.json")
    hard_path = os.path.abspath(hard_path)
    if os.path.exists(hard_path):
        with open(hard_path) as f:
            report["hard_tier2"] = json.load(f)

    return report


# ---------- AP2 (Agent Payments Protocol) compatibility ----------
# See app/core/ap2_adapter.py for what this does and does not cover.
# This lets an AP2-speaking caller (Intent Mandate + Cart Mandate) use
# Sentinel as its authorization layer without knowing Sentinel's
# internal schema -- concrete evidence of protocol compatibility, not
# just a conceptual claim.

class AP2EvaluateIn(BaseModel):
    agent_id: str
    owner_human_id: str
    intent_mandate: dict
    cart_mandate: dict


@secured.post("/ap2/evaluate-mandate")
def ap2_evaluate_mandate(body: AP2EvaluateIn, db: Session = Depends(get_session)):
    from app.core.ap2_adapter import translate_mandates

    translation = translate_mandates(
        intent_mandate=body.intent_mandate,
        cart_mandate=body.cart_mandate,
        agent_id=body.agent_id,
        owner_human_id=body.owner_human_id,
    )

    orchestrator = SentinelOrchestrator(db)
    decision = orchestrator.evaluate_transaction(translation.policy, translation.request, scenario_tag="ap2")

    return {
        "decision": decision.__dict__,
        "ap2_signature_verified": translation.signature_verified,
        "warnings": translation.warnings,
        "note": (
            "A verdict of APPROVE here is what would inform generating an AP2 Payment Mandate "
            "in a full integration. This endpoint translates mandate JSON shape only -- it does "
            "not verify AP2's cryptographic signatures (W3C Verifiable Credentials); see "
            "app/core/ap2_adapter.py for exactly what is and isn't covered."
        ),
    }


@app.get("/health")
def health():
    """Actually checks dependencies rather than just returning 200 --
    a health check that can't distinguish 'the process is up' from 'the
    process can do its job' is worse than no health check, since a load
    balancer or orchestrator will happily route traffic to a replica
    that can't reach its database."""
    instance_id = os.environ.get("SENTINEL_INSTANCE_ID", "unknown")
    checks = {}

    try:
        from sqlalchemy import text
        db = SessionLocal()
        try:
            db.execute(text("SELECT 1"))
            checks["database"] = "ok"
        finally:
            db.close()
    except Exception as e:
        checks["database"] = f"unreachable: {e}"

    try:
        queue_stats = eval_queue.stats()
        checks["queue"] = queue_stats.get("backend", "memory")
    except Exception as e:
        checks["queue"] = f"error: {e}"

    healthy = checks.get("database") == "ok"
    body = {"status": "ok" if healthy else "degraded", "instance_id": instance_id, "checks": checks}
    if not healthy:
        return JSONResponse(status_code=503, content=body)
    return body


app.include_router(secured)

"""Scoped API for policy administration, agent requests and human review."""

from app.core.env import load_env

load_env()
from contextlib import asynccontextmanager
from datetime import timedelta, timezone
from typing import Optional
import asyncio
import json
import logging
import os
import secrets
import uuid
import time
from fastapi import FastAPI, APIRouter, Depends, HTTPException, Request, Query, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from fastapi.exceptions import RequestValidationError
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.middleware.trustedhost import TrustedHostMiddleware
from app.core.http_limits import RequestLimitsMiddleware
from sqlalchemy import func, text
from sqlalchemy.orm import Session, joinedload, selectinload
from sqlalchemy.exc import SQLAlchemyError
from pydantic import BaseModel, Field
from app.models.db import (
    get_session,
    SessionLocal,
    TransactionRecord,
    DecisionRecord,
    PolicyRecord,
    BrowserSession,
    PaymentRecord,
)
from app.models.policy import AgentPolicy, TransactionRequest, TimeWindow, VelocityLimit
from app.core.auth import (
    Principal,
    require_api_key,
    require_agent,
    authenticate,
    principal_for_subject,
    validate_authority,
    assert_current_principal,
    digest,
)
from app.core.rate_limit import enforce_rate_limit
from app.core.logging_middleware import StructuredLoggingMiddleware
from app.core.locking import agent_transaction
from app.core.intelligence import processing_time
from app.core.orchestrator import SentinelOrchestrator, ConflictError
from app.core.schemas import (
    PolicyIn,
    TransactionIn,
    BatchEvaluateIn,
    HoldResolutionIn,
    SimulateIn,
    PaymentCancelIn,
    ReconcileIn,
)
from app.core.redis_queue import make_queue
from app.core.settings import deployment_dispatch_paused, integer
from app.schema_version import SCHEMA_REVISION
from app.core.queue_errors import QueueCapacityError
from app.agents.purchasing_agent import PurchasingAgent

logger = logging.getLogger("sentinel.api")


def _process_single_evaluation(payload):
    payload = dict(payload)
    scope = payload.pop("_submitted_by", None)
    work_lease = payload.pop("_job_lease", None)
    submitted_version = payload.pop("_submitted_version", 1)
    if scope:
        principal = principal_for_subject(scope)
        if principal is None or principal.credential_version != submitted_version:
            raise ValueError("Submitting credential has been revoked or rotated")
    scenario = payload.pop("scenario_tag", "")
    db = SessionLocal()
    try:
        policy = load_policy(payload["agent_id"], db)
        if scope:
            principal.require("admin", "owner", "agent")
            require_agent(principal, payload["agent_id"], db)
        request = TransactionRequest(**payload)
        # Queued requests are evaluated against the latest trusted goal and clock.
        if not scenario:
            request.raw_agent_goal = policy.authorized_goal
        return (
            SentinelOrchestrator(db)
            .evaluate_transaction(
                policy,
                request,
                scenario,
                actor_id=scope or "internal",
                work_lease=work_lease,
                actor_version=submitted_version,
            )
            .__dict__
        )
    finally:
        db.close()


eval_queue = make_queue(
    _process_single_evaluation,
    num_workers=integer("SENTINEL_QUEUE_WORKERS", 4, maximum=16),
)


async def recover_outbox(stop):
    def load():
        from app.core.payment_recovery import recover_stale_dispatches

        with SessionLocal() as db:
            recover_stale_dispatches(db)
            return [
                r[0]
                for r in db.query(PaymentRecord.request_id)
                .filter_by(state="pending")
                .order_by(
                    PaymentRecord.updated_at.asc(), PaymentRecord.request_id.asc()
                )
                .limit(8)
            ]

    def dispatch_one(rid):
        with SessionLocal() as db:
            SentinelOrchestrator(db).dispatch_payment(rid)

    while not stop.is_set():
        try:
            ids = await asyncio.to_thread(load)
            # Separate sessions per task; bounded parallel IO does not let one
            # slow provider response stall an entire recovery batch.
            for start in range(0, len(ids), 4):
                if stop.is_set():
                    break
                await asyncio.gather(
                    *(
                        asyncio.to_thread(dispatch_one, rid)
                        for rid in ids[start : start + 4]
                    )
                )
            app.state.outbox_last_tick = time.monotonic()
        except Exception:
            logger.exception("Payment outbox recovery failed")
        try:
            await asyncio.wait_for(stop.wait(), timeout=5)
        except TimeoutError:
            pass


async def operations_loop(stop):
    from app.core.operations import detect, deliver_one

    def cycle():
        quota_ok = True
        if os.environ.get("RATE_LIMIT_BACKEND") == "redis":
            try:
                from app.core.rate_limit import shared_client

                shared_client(
                    os.environ.get("REDIS_URL", "redis://localhost:6379/0")
                ).ping()
            except Exception:
                quota_ok = False
        with SessionLocal() as db:
            from app.core.provider_refresh import scheduler_enabled

            worker = getattr(app.state, "providers_task", None)
            provider_ok = not scheduler_enabled() or (
                worker is not None
                and not worker.done()
                and time.monotonic() - getattr(app.state, "providers_last_tick", 0)
                < integer("SENTINEL_PROVIDER_POLL_SECONDS", 15, maximum=300) + 150
            )
            detect(db, quota_ok, provider_worker_ok=provider_ok)
            for _ in range(5):
                if not deliver_one(db):
                    break

    while not stop.is_set():
        try:
            await asyncio.to_thread(cycle)
            app.state.operations_last_tick = time.monotonic()
        except Exception as exc:
            logger.warning("Operations monitor unavailable (%s)", type(exc).__name__)
        try:
            await asyncio.wait_for(
                stop.wait(),
                timeout=integer("SENTINEL_MONITOR_INTERVAL_SECONDS", 30, maximum=300),
            )
        except TimeoutError:
            pass


async def provider_refresh_loop(stop):
    from app.core.provider_refresh import run_one, RefreshConflict

    def cycle():
        with SessionLocal() as db:
            for _ in range(2):
                if stop.is_set():
                    break
                try:
                    if run_one(db) is None:
                        break
                except RefreshConflict:
                    db.rollback()  # A newer binding/lease is authoritative.

    while not stop.is_set():
        try:
            await asyncio.to_thread(cycle)
            app.state.providers_last_tick = time.monotonic()
        except Exception as exc:
            logger.warning(
                "Provider refresh worker unavailable (%s)", type(exc).__name__
            )
        try:
            await asyncio.wait_for(
                stop.wait(),
                timeout=integer("SENTINEL_PROVIDER_POLL_SECONDS", 15, maximum=300),
            )
        except TimeoutError:
            pass


@asynccontextmanager
async def lifespan(app):
    from app.core.env import validate_environment

    validate_environment(serving=True)
    deployment_dispatch_paused()  # Reject invalid restore controls before workers start.
    validate_authority()  # fail closed on invalid or empty shared authority
    from app.core.llm_client import LLMClient
    from app.core.razorpay_gateway import RazorpayGateway

    if os.environ.get("SENTINEL_ENV") == "production":
        from app.models.db import engine

        if engine.dialect.name != "postgresql":
            raise RuntimeError("Production profile requires PostgreSQL")
        if LLMClient().mock_mode:
            raise RuntimeError(
                "Production profile requires an explicitly configured live reasoning provider"
            )
        RazorpayGateway()
        from app.core.settings import validate_production_transport

        validate_production_transport(
            os.environ.get("SENTINEL_DATABASE_URL", ""), os.environ.get("REDIS_URL", "")
        )
        if os.environ.get("SENTINEL_MIGRATION_DATABASE_URL") or os.environ.get(
            "SENTINEL_RUNTIME_DB_PASSWORD"
        ):
            raise RuntimeError(
                "Migration/provisioning credentials must not be mounted in the runtime service"
            )
        origins = os.environ.get("SENTINEL_ALLOWED_ORIGINS", "").split(",")
        if not origins or any(
            not o.startswith("https://") or "*" in o for o in origins
        ):
            raise RuntimeError(
                "Production profile requires explicit HTTPS browser origins"
            )
    if os.environ.get("SENTINEL_ENV") == "production":
        if os.environ.get("QUEUE_BACKEND") != "postgres":
            raise RuntimeError("Production requires QUEUE_BACKEND=postgres")
        if os.environ.get("RATE_LIMIT_BACKEND") != "redis":
            raise RuntimeError("Production requires RATE_LIMIT_BACKEND=redis")
        hosts = os.environ.get("SENTINEL_ALLOWED_HOSTS", "").split(",")
        if not hosts or any(not h or "*" in h for h in hosts):
            raise RuntimeError("Production requires explicit SENTINEL_ALLOWED_HOSTS")
        if len(os.environ.get("RAZORPAY_WEBHOOK_SECRET", "")) < 32:
            raise RuntimeError("Production requires a 32+ character webhook secret")
        if not os.environ.get("LLM_MODEL"):
            raise RuntimeError("Production requires an explicit LLM_MODEL")
    if (
        os.environ.get("SENTINEL_ENV") == "production"
        or os.environ.get("SENTINEL_REQUIRE_MIGRATIONS") == "1"
    ):
        with SessionLocal() as db:
            if (
                os.environ.get("SENTINEL_ENV") == "production"
                or os.environ.get("SENTINEL_REQUIRE_RESTRICTED_DB") == "1"
            ):
                from app.ops.db_privileges import validate

                validate(db)
            version = db.execute(
                text("SELECT version_num FROM alembic_version")
            ).scalar_one()
            if version != SCHEMA_REVISION:
                raise RuntimeError(
                    "Apply the current Alembic migration before serving traffic"
                )
    if (
        os.environ.get("QUEUE_BACKEND") == "postgres"
        and os.environ.get("RATE_LIMIT_BACKEND") == "redis"
    ):
        from app.core.rate_limit import shared_client

        client = shared_client(os.environ.get("REDIS_URL", "redis://localhost:6379/0"))
        if await asyncio.to_thread(client.xlen, "sentinel:queue:v2"):
            raise RuntimeError(
                "Drain accepted legacy Redis jobs before switching to the PostgreSQL queue"
            )
    from app.core.operations import endpoint

    from app.core.providers import required_feeds
    from app.core.provider_refresh import scheduler_enabled

    required_feeds()
    scheduled_providers = scheduler_enabled()
    configured_delivery = endpoint()
    if os.environ.get("SENTINEL_ENV") == "production" and configured_delivery is None:
        raise RuntimeError(
            "Production requires a configured operational alert destination"
        )
    await eval_queue.start()
    stop = asyncio.Event()
    task = asyncio.create_task(recover_outbox(stop))
    app.state.outbox_task = task
    app.state.outbox_last_tick = time.monotonic()
    monitor = asyncio.create_task(operations_loop(stop))
    app.state.operations_task = monitor
    app.state.operations_last_tick = time.monotonic()
    background = [task, monitor]
    app.state.providers_task = None
    if scheduled_providers:
        worker = asyncio.create_task(provider_refresh_loop(stop))
        app.state.providers_task = worker
        app.state.providers_last_tick = time.monotonic()
        background.append(worker)
    try:
        yield
    finally:
        stop.set()
        try:
            await asyncio.wait_for(
                asyncio.gather(
                    *(asyncio.shield(t) for t in background), eval_queue.stop()
                ),
                timeout=integer("SENTINEL_SHUTDOWN_SECONDS", 30, maximum=60) + 2,
            )
        except TimeoutError:
            for t in background:
                t.cancel()
            await asyncio.gather(*background, return_exceptions=True)


app = FastAPI(title="Sentinel API", lifespan=lifespan)
app.add_middleware(
    CORSMiddleware,
    allow_origins=os.environ.get(
        "SENTINEL_ALLOWED_ORIGINS", "http://localhost:5173,http://127.0.0.1:5173"
    ).split(","),
    allow_credentials=True,
    allow_methods=["GET", "POST"],
    allow_headers=["Content-Type", "X-API-Key", "X-Sentinel-CSRF", "X-Request-ID"],
)
app.add_middleware(RequestLimitsMiddleware)
app.add_middleware(
    TrustedHostMiddleware,
    allowed_hosts=os.environ.get("SENTINEL_ALLOWED_HOSTS", "*").split(","),
)
app.add_middleware(StructuredLoggingMiddleware)


@app.exception_handler(StarletteHTTPException)
async def http_error(request, exc):
    return JSONResponse(
        status_code=exc.status_code,
        headers=exc.headers,
        content={"error": {"message": exc.detail, "status_code": exc.status_code}},
    )


@app.exception_handler(RequestValidationError)
async def validation_error(request, exc):
    details = [
        {k: v for k, v in e.items() if k in ("loc", "msg", "type")}
        for e in exc.errors()
    ]
    return JSONResponse(
        status_code=422,
        content={
            "error": {
                "message": "Invalid request",
                "status_code": 422,
                "details": details,
            }
        },
    )


@app.exception_handler(QueueCapacityError)
async def queue_full(request, exc):
    return JSONResponse(
        status_code=503,
        content={"error": {"message": str(exc), "status_code": 503}},
        headers={"Retry-After": "1"},
    )


@app.exception_handler(ConflictError)
async def conflict(request, exc):
    return JSONResponse(
        status_code=409, content={"error": {"message": str(exc), "status_code": 409}}
    )


@app.exception_handler(ValueError)
async def value_error(request, exc):
    return JSONResponse(
        status_code=422, content={"error": {"message": str(exc), "status_code": 422}}
    )


@app.exception_handler(SQLAlchemyError)
async def database_error(request, exc):
    logger.exception("Database error")
    return JSONResponse(
        status_code=503,
        content={
            "error": {"message": "Database temporarily unavailable", "status_code": 503}
        },
        headers={"Retry-After": "1"},
    )


@app.exception_handler(Exception)
async def internal_error(request, exc):
    logger.exception("Unhandled request error")
    return JSONResponse(
        status_code=500,
        content={"error": {"message": "Internal server error", "status_code": 500}},
    )


secured = APIRouter(
    dependencies=[Depends(require_api_key), Depends(enforce_rate_limit)]
)


class LoginIn(BaseModel):
    credential: str = Field(min_length=1, max_length=512)


@app.post("/auth/login", dependencies=[Depends(enforce_rate_limit)])
def login(
    body: LoginIn,
    response: Response,
    request: Request,
    db: Session = Depends(get_session),
):
    allowed = os.environ.get(
        "SENTINEL_ALLOWED_ORIGINS", "http://localhost:5173,http://127.0.0.1:5173"
    ).split(",")
    if request.headers.get("Origin") not in allowed:
        raise HTTPException(403, "Invalid origin")
    principal = authenticate(body.credential)
    if principal.role == "agent":
        raise HTTPException(403, "Agent credentials cannot create browser sessions")
    token = secrets.token_urlsafe(32)
    now = processing_time(db)
    db.query(BrowserSession).filter(BrowserSession.expires_at < now).delete()
    db.add(
        BrowserSession(
            token_hash=digest(token),
            credential_hash=digest(body.credential),
            expires_at=now + timedelta(hours=8),
        )
    )
    db.commit()
    response.set_cookie(
        "sentinel_session",
        token,
        httponly=True,
        secure=os.environ.get("SENTINEL_ENV") == "production",
        samesite="strict",
        max_age=28800,
    )
    return {
        "subject": principal.subject,
        "role": principal.role,
        "owner_id": principal.owner_id,
    }


@secured.get("/auth/me")
def whoami(principal: Principal = Depends(require_api_key)):
    return {
        "subject": principal.subject,
        "role": principal.role,
        "owner_id": principal.owner_id,
    }


@secured.post("/auth/logout")
def logout(request: Request, response: Response, db: Session = Depends(get_session)):
    db.query(BrowserSession).filter_by(
        token_hash=digest(request.cookies.get("sentinel_session", ""))
    ).delete()
    db.commit()
    response.delete_cookie("sentinel_session")
    return {"status": "signed_out"}


def load_policy(agent_id, db):
    row = (
        db.query(PolicyRecord)
        .filter_by(agent_id=agent_id, active=True)
        .order_by(PolicyRecord.version.desc())
        .first()
    )
    if not row:
        raise HTTPException(404, "No active policy for this agent")
    return AgentPolicy.from_dict(row.policy_json)


def scoped(query, principal, db):
    if principal.role == "admin":
        return query
    if principal.role == "agent":
        return query.filter(TransactionRecord.agent_id.in_(principal.agent_ids))
    return query.filter(
        TransactionRecord.agent_id.in_(
            db.query(PolicyRecord.agent_id).filter_by(owner_human_id=principal.owner_id)
        )
    )


@secured.post("/policies")
def create_policy(
    body: PolicyIn,
    db: Session = Depends(get_session),
    principal: Principal = Depends(require_api_key),
):
    principal.require("admin", "owner")
    if principal.role != "admin" and body.owner_human_id != principal.owner_id:
        raise HTTPException(403, "Cannot assign another owner")
    with agent_transaction(db, body.agent_id):
        assert_current_principal(principal, db)
        existing = db.query(PolicyRecord).filter_by(agent_id=body.agent_id).all()
        if any(r.owner_human_id != body.owner_human_id for r in existing):
            raise HTTPException(403, "Agent ownership cannot be reassigned")
        version = max((r.version for r in existing), default=0) + 1
        for row in existing:
            row.active = False
        db.flush()
        policy = AgentPolicy(
            policy_id=f"pol_{uuid.uuid4().hex}",
            agent_id=body.agent_id,
            owner_human_id=body.owner_human_id,
            version=version,
            authorized_goal=body.authorized_goal,
            max_amount_per_txn=body.max_amount_per_txn,
            max_daily_total=body.max_daily_total,
            allowed_categories=body.allowed_categories,
            blocked_merchants=body.blocked_merchants,
            allowed_merchants=body.allowed_merchants,
            time_window=TimeWindow(body.time_window_start, body.time_window_end),
            velocity=VelocityLimit(
                body.velocity_max_count, body.velocity_window_minutes
            ),
        )
        db.add(
            PolicyRecord(
                policy_id=policy.policy_id,
                agent_id=policy.agent_id,
                owner_human_id=policy.owner_human_id,
                version=version,
                active=True,
                policy_json=policy.to_dict(),
            )
        )
    return {"policy_id": policy.policy_id, "policy": policy.to_dict()}


@secured.get("/policies/{agent_id}")
def get_latest_policy(
    agent_id: str,
    db: Session = Depends(get_session),
    principal: Principal = Depends(require_api_key),
):
    require_agent(principal, agent_id, db)
    policy = load_policy(agent_id, db)
    return {
        "policy_id": policy.policy_id,
        "policy": policy.to_dict(),
        "version": policy.version,
    }


@secured.get("/policies")
def list_policies(
    db: Session = Depends(get_session), principal: Principal = Depends(require_api_key)
):
    q = db.query(PolicyRecord).filter_by(active=True)
    if principal.role == "agent":
        q = q.filter(PolicyRecord.agent_id.in_(principal.agent_ids))
    elif principal.role != "admin":
        q = q.filter_by(owner_human_id=principal.owner_id)
    return [
        {"policy_id": r.policy_id, "agent_id": r.agent_id, "policy": r.policy_json}
        for r in q.all()
    ]


def _serialize_transaction(tx: TransactionRecord) -> dict:
    decision = tx.decision  # populated via joinedload -- no extra query per row
    return {
        "request_id": tx.request_id,
        "agent_id": tx.agent_id,
        "merchant_name": tx.merchant_name,
        "merchant_id": tx.merchant_id,
        "merchant_version": tx.merchant_version,
        "merchant_category": tx.merchant_category,
        "amount": float(tx.amount),
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
        "risk_score": decision.risk_score if decision else None,
        "intelligence": tx.intelligence.report if tx.intelligence else None,
        "policy_version": decision.policy_version if decision else None,
        "llm_mode": decision.llm_mode if decision else None,
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
        "created_at_iso": tx.created_at.replace(tzinfo=timezone.utc).isoformat()
        if tx.created_at
        else None,
    }


@secured.get("/transactions")
def list_transactions(
    limit: Optional[int] = Query(default=None, ge=1, le=200),
    page: Optional[int] = Query(default=None, ge=1),
    page_size: int = Query(default=25, ge=1, le=200),
    agent_id: Optional[str] = None,
    verdict: Optional[str] = None,
    search: Optional[str] = Query(default=None, max_length=200),
    scenario_tag: Optional[str] = None,
    db: Session = Depends(get_session),
    principal: Principal = Depends(require_api_key),
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
    query = scoped(
        db.query(TransactionRecord).options(
            joinedload(TransactionRecord.decision),
            selectinload(TransactionRecord.intelligence),
        ),
        principal,
        db,
    )

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
        (TransactionRecord.scenario_tag.is_(None))
        | (
            ~TransactionRecord.scenario_tag.regexp_match(
                r"^(golden_|fuzz|loadtest|eval:)"
            )
        )
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
        query = query.join(
            DecisionRecord, DecisionRecord.request_id == TransactionRecord.request_id
        ).filter(DecisionRecord.verdict == verdict.upper())

    query = query.order_by(
        TransactionRecord.created_at.desc(), TransactionRecord.id.desc()
    )

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
def list_scenario_tags(
    db: Session = Depends(get_session), principal: Principal = Depends(require_api_key)
):
    """Distinct scenario tags actually present, for building filter UI --
    without shipping every transaction row to the client just to
    dedupe a small set of tags client-side."""
    rows = (
        scoped(db.query(TransactionRecord.scenario_tag), principal, db)
        .filter(
            TransactionRecord.scenario_tag.isnot(None),
            TransactionRecord.scenario_tag != "",
        )
        .filter(
            ~TransactionRecord.scenario_tag.regexp_match(
                r"^(golden_|fuzz|loadtest|eval:)"
            )
        )  # never NULL here, filtered above
        .distinct()
        .all()
    )
    return sorted(r[0] for r in rows)


@secured.get("/transactions/summary")
def transactions_summary(
    agent_id: Optional[str] = None,
    db: Session = Depends(get_session),
    principal: Principal = Depends(require_api_key),
):
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
        .join(
            TransactionRecord, TransactionRecord.request_id == DecisionRecord.request_id
        )
        .filter(
            (TransactionRecord.scenario_tag.is_(None))
            | (
                ~TransactionRecord.scenario_tag.regexp_match(
                    r"^(golden_|fuzz|loadtest|eval:)"
                )
            )
        )
    )
    if agent_id:
        query = query.filter(TransactionRecord.agent_id == agent_id)
    query = scoped(query, principal, db)
    rows = query.group_by(DecisionRecord.verdict).all()
    by_verdict = {"APPROVE": 0, "HOLD": 0, "BLOCK": 0}
    total = 0
    for verdict, count in rows:
        if verdict in by_verdict:
            by_verdict[verdict] = count
        total += count
    return {"total": total, "by_verdict": by_verdict}


@secured.post("/transactions/evaluate")
def evaluate_transaction(
    body: TransactionIn,
    db: Session = Depends(get_session),
    principal: Principal = Depends(require_api_key),
):
    principal.require("admin", "owner", "agent")
    require_agent(principal, body.agent_id, db)
    policy = load_policy(body.agent_id, db)
    # Replays retain server-assigned event time; client timestamps never overwrite it.
    existing = db.query(TransactionRecord).filter_by(request_id=body.request_id).first()
    request = body.domain(policy)
    if existing:
        request.requested_at_iso, request.requested_at_hour = (
            existing.requested_at_iso,
            existing.requested_at_hour,
        )
        request.raw_agent_goal = existing.raw_agent_goal
    return (
        SentinelOrchestrator(db)
        .evaluate_transaction(
            policy,
            request,
            actor_id=principal.subject,
            actor_version=principal.credential_version,
        )
        .__dict__
    )


@secured.post("/holds/{request_id}/resolve")
def resolve_hold(
    request_id: str,
    body: HoldResolutionIn,
    db: Session = Depends(get_session),
    principal: Principal = Depends(require_api_key),
):
    principal.require("admin", "owner", "reviewer")
    tx = db.query(TransactionRecord).filter_by(request_id=request_id).first()
    if not tx:
        raise HTTPException(404, "Unknown request_id")
    require_agent(principal, tx.agent_id, db)
    return SentinelOrchestrator(db).resolve_hold(
        request_id,
        body.human_verdict,
        reviewer_id=principal.subject,
        reason=body.reason,
        reviewer_principal=principal,
    )


def prepare_batch(body, principal):
    principal.require("admin", "owner", "agent")
    with SessionLocal() as db:
        payloads = []
        for item in body.requests:
            require_agent(principal, item.agent_id, db)
            payload = item.domain(load_policy(item.agent_id, db)).__dict__
            payload["_submitted_by"] = principal.subject
            payload["_submitted_version"] = principal.credential_version
            payloads.append(payload)
        return payloads


@secured.post("/transactions/evaluate-batch")
async def evaluate_batch(
    body: BatchEvaluateIn, principal: Principal = Depends(require_api_key)
):
    payloads = await asyncio.to_thread(prepare_batch, body, principal)
    batch_id = f"batch_{uuid.uuid4().hex}"
    ids = await eval_queue.enqueue_batch(payloads, batch_id)
    return {"batch_id": batch_id, "job_ids": ids, "queued": len(ids)}


def scenario_requests(body, db, principal):
    principal.require("admin", "owner")
    if os.environ.get("SENTINEL_ENV") == "production":
        raise HTTPException(403, "Simulation is disabled in production")
    require_agent(principal, body.agent_id, db)
    policy = load_policy(body.agent_id, db)
    requests = PurchasingAgent().run(
        body.agent_id,
        policy.authorized_goal,
        body.mode,
        body.drift_type,
        body.num_transactions,
        policy=policy,
    )
    tag = (
        "well_behaved"
        if body.mode == "well_behaved"
        else f"adversarial:{body.drift_type}"
    )
    return policy, requests, tag


@secured.post("/simulate")
def simulate(
    body: SimulateIn,
    db: Session = Depends(get_session),
    principal: Principal = Depends(require_api_key),
):
    policy, requests, tag = scenario_requests(body, db, principal)
    return {
        "scenario_tag": tag,
        "results": [
            {
                "request": r.__dict__,
                "decision": SentinelOrchestrator(db)
                .evaluate_transaction(policy, r, tag)
                .__dict__,
            }
            for r in requests
        ],
    }


def prepare_simulation(body, principal):
    with SessionLocal() as db:
        _, requests, tag = scenario_requests(body, db, principal)
        return [
            dict(
                r.__dict__,
                scenario_tag=tag,
                _submitted_by=principal.subject,
                _submitted_version=principal.credential_version,
            )
            for r in requests
        ], tag


@secured.post("/simulate-async")
async def simulate_async(
    body: SimulateIn, principal: Principal = Depends(require_api_key)
):
    payloads, tag = await asyncio.to_thread(prepare_simulation, body, principal)
    batch_id = f"batch_{uuid.uuid4().hex}"
    ids = await eval_queue.enqueue_batch(payloads, batch_id)
    return {"batch_id": batch_id, "job_ids": ids, "scenario_tag": tag}


@secured.get("/eval/report")
def eval_report(principal: Principal = Depends(require_api_key)):
    principal.require("admin", "owner", "reviewer")
    from pathlib import Path

    data = Path(__file__).resolve().parents[2] / "data"
    path = data / "eval_report.json"
    if not path.exists():
        raise HTTPException(404, "No evaluation report")
    report = json.loads(path.read_text())
    if (data / "hard_eval_report.json").exists():
        report["hard_tier2"] = json.loads((data / "hard_eval_report.json").read_text())
    return report


@secured.post("/ap2/evaluate-mandate")
def ap2_disabled(principal: Principal = Depends(require_api_key)):
    # Approximate unsigned JSON cannot confer payment authority.
    raise HTTPException(
        501,
        "Authenticated AP2 integration is not implemented; use the scoped transaction API",
    )


@secured.get("/jobs")
def queue_stats(principal: Principal = Depends(require_api_key)):
    principal.require("admin")
    return eval_queue.stats()


def visible_job(job, principal, db):
    if not isinstance(job, dict):
        job = dict(job.__dict__, status=job.status.value)
    if principal.role != "admin" and job.get("submitted_by") != principal.subject:
        raise HTTPException(403, "Job outside credential scope")
    if principal.role != "admin":
        if not job.get("agent_id"):
            raise HTTPException(403, "Legacy job has no verifiable agent scope")
        require_agent(principal, job["agent_id"], db)
    return {k: v for k, v in job.items() if k not in ("submitted_by", "completed_at")}


@secured.get("/jobs/{job_id}")
def job_status(
    job_id: str,
    principal: Principal = Depends(require_api_key),
    db: Session = Depends(get_session),
):
    job = eval_queue.get_job(job_id)
    if job is None:
        raise HTTPException(404, "Unknown job_id")
    return visible_job(job, principal, db)


@secured.get("/jobs/batch/{batch_id}")
def batch_status(
    batch_id: str,
    principal: Principal = Depends(require_api_key),
    db: Session = Depends(get_session),
):
    batch = eval_queue.get_batch(batch_id)
    if batch is None:
        raise HTTPException(404, "Unknown batch_id")
    batch["jobs"] = [visible_job(job, principal, db) for job in batch["jobs"]]
    return batch


@app.get("/health/live")
async def liveness():
    return {"status": "alive"}


@app.get("/health/ready")
@app.get("/health")
def health():
    checks = {}
    db = SessionLocal()
    try:
        db.execute(text("SELECT 1"))
        checks["database"] = "ok"
    except Exception:
        checks["database"] = "unavailable"
    finally:
        db.close()
    try:
        stats = eval_queue.stats()
        checks["queue"] = stats.get("backend", "memory")
        checks["workers"] = "ok" if stats.get("live_workers", 0) > 0 else "unavailable"
    except Exception:
        checks["queue"], checks["workers"] = "unavailable", "unavailable"
    if os.environ.get("RATE_LIMIT_BACKEND") == "redis":
        try:
            from app.core.rate_limit import shared_client

            shared_client(
                os.environ.get("REDIS_URL", "redis://localhost:6379/0")
            ).ping()
            checks["quota"] = "ok"
        except Exception:
            checks["quota"] = "unavailable"
    outbox = getattr(app.state, "outbox_task", None)
    checks["outbox"] = (
        "ok"
        if outbox is not None
        and not outbox.done()
        and time.monotonic() - getattr(app.state, "outbox_last_tick", 0) < 120
        else "unavailable"
    )
    monitor = getattr(app.state, "operations_task", None)
    checks["operations"] = (
        "ok"
        if monitor is not None
        and not monitor.done()
        and time.monotonic() - getattr(app.state, "operations_last_tick", 0)
        < integer("SENTINEL_MONITOR_INTERVAL_SECONDS", 30, maximum=300) + 120
        else "unavailable"
    )
    from app.core.provider_refresh import scheduler_enabled

    checks["provider_refresh"] = "disabled"
    if scheduler_enabled():
        worker = getattr(app.state, "providers_task", None)
        checks["provider_refresh"] = (
            "ok"
            if worker is not None
            and not worker.done()
            and time.monotonic() - getattr(app.state, "providers_last_tick", 0)
            < integer("SENTINEL_PROVIDER_POLL_SECONDS", 15, maximum=300) + 150
            else "unavailable"
        )
    ok = (
        checks.get("database") == "ok"
        and checks.get("workers") == "ok"
        and checks["outbox"] == "ok"
        and checks["operations"] == "ok"
        and checks.get("quota") != "unavailable"
    )
    return JSONResponse(
        status_code=200 if ok else 503,
        content={
            "status": "ok" if ok else "degraded",
            "instance_id": os.environ.get("SENTINEL_INSTANCE_ID", "local"),
            "checks": checks,
        },
    )


@app.post(
    "/payments/{request_id}/reconcile",
    dependencies=[Depends(require_api_key), Depends(enforce_rate_limit)],
)
def reconcile_payment(
    request_id: str,
    body: ReconcileIn,
    db: Session = Depends(get_session),
    principal: Principal = Depends(require_api_key),
):
    principal.require("admin")
    payment = db.get(PaymentRecord, request_id)
    if not payment:
        raise HTTPException(404, "No payment reservation")
    amount_paise = payment.amount_paise
    merchant_id = (
        db.query(TransactionRecord.merchant_id)
        .filter_by(request_id=request_id)
        .scalar()
        or ""
    )
    db.rollback()
    result = SentinelOrchestrator(db).gateway.reconcile(
        body.order_id, request_id, amount_paise, merchant_id=merchant_id
    )
    from app.core.intelligence import GUARD_LOCK, control_event

    with agent_transaction(db, GUARD_LOCK):
        assert_current_principal(principal, db)
        payment = (
            db.query(PaymentRecord)
            .filter_by(request_id=request_id)
            .populate_existing()
            .with_for_update()
            .one()
        )
        if payment.order_id and payment.order_id != result["order_id"]:
            raise HTTPException(409, "Payment is already bound to another order")
        if payment.state == "captured" and result["status"] != "captured":
            raise HTTPException(409, "Cannot downgrade captured state")
        before = dict(state=payment.state, order_id=payment.order_id)
        payment.state, payment.order_id = result["status"], result["order_id"]
        payment.updated_at = processing_time(db)
        decision = db.query(DecisionRecord).filter_by(request_id=request_id).one()
        decision.razorpay_status, decision.razorpay_ref = (
            payment.state,
            payment.order_id,
        )
        control_event(
            db,
            principal.subject,
            "payment_reconciliation",
            request_id,
            before,
            dict(state=payment.state, order_id=payment.order_id),
        )

    return result


@app.post(
    "/payments/{request_id}/cancel",
    dependencies=[Depends(require_api_key), Depends(enforce_rate_limit)],
)
def cancel_pending_payment(
    request_id: str,
    body: PaymentCancelIn,
    db: Session = Depends(get_session),
    principal: Principal = Depends(require_api_key),
):
    from app.core.intelligence import GUARD_LOCK, control_event

    principal.require("admin", "owner")
    tx = db.query(TransactionRecord).filter_by(request_id=request_id).first()
    if tx is None:
        raise HTTPException(404, "Unknown request")
    require_agent(principal, tx.agent_id, db)
    with agent_transaction(db, GUARD_LOCK):
        assert_current_principal(principal, db)
        payment = (
            db.query(PaymentRecord)
            .filter_by(request_id=request_id)
            .populate_existing()
            .with_for_update()
            .first()
        )
        if payment is None or payment.state != "pending":
            raise ConflictError("Only an undispatched pending payment can be cancelled")
        payment.state, payment.error = "cancelled", body.reason
        payment.updated_at = processing_time(db)
        db.query(DecisionRecord).filter_by(
            request_id=request_id
        ).one().razorpay_status = "cancelled"
        control_event(
            db,
            principal.subject,
            "payment_cancel",
            request_id,
            {"state": "pending"},
            {"state": "cancelled", "reason": body.reason},
        )
    return {
        "status": "cancelled",
        "note": "Undispatched reservation released; original decision retained",
    }


@app.get(
    "/payments/reconciliation",
    dependencies=[Depends(require_api_key), Depends(enforce_rate_limit)],
)
def pending_reconciliation(
    db: Session = Depends(get_session), principal: Principal = Depends(require_api_key)
):
    principal.require("admin")
    return [
        {"request_id": p.request_id, "state": p.state, "order_id": p.order_id}
        for p in db.query(PaymentRecord)
        .filter(PaymentRecord.state.in_(["unknown", "processing"]))
        .limit(200)
        .all()
    ]


@app.post("/payments/webhook")
async def provider_webhook(request: Request):
    raw = await request.body()
    return await asyncio.to_thread(
        record_provider_event,
        raw,
        request.headers.get("X-Razorpay-Signature", ""),
        request.headers.get("X-Razorpay-Event-Id", ""),
    )


def record_provider_event(raw, signature, event_id):
    with SessionLocal() as db:
        import hashlib
        import hmac
        from app.models.db import ProviderEvent

        secret = os.environ.get("RAZORPAY_WEBHOOK_SECRET", "")
        if len(raw) > 65536:
            raise HTTPException(413, "Webhook too large")
        import re

        if (
            not secret
            or not re.fullmatch(r"[a-fA-F0-9]{64}", signature)
            or not hmac.compare_digest(
                hmac.new(secret.encode(), raw, hashlib.sha256).hexdigest(), signature
            )
        ):
            raise HTTPException(401, "Invalid webhook signature")
        try:
            event = json.loads(raw)
        except (ValueError, UnicodeDecodeError):
            raise HTTPException(422, "Invalid webhook JSON") from None
        if not isinstance(event, dict):
            raise HTTPException(422, "Invalid webhook event")
        event_id = event_id or hashlib.sha256(raw).hexdigest()
        if len(event_id) > 128:
            raise HTTPException(422, "Invalid provider event ID")
        if event.get("event") != "payment.captured":
            return {"status": "ignored"}
        try:
            entity = event["payload"]["payment"]["entity"]
            if (
                not isinstance(entity, dict)
                or type(entity.get("amount")) is not int
                or not isinstance(entity.get("order_id"), str)
            ):
                raise ValueError("Invalid capture entity")
        except (KeyError, TypeError, ValueError):
            raise HTTPException(422, "Invalid capture entity") from None
        payment = (
            db.query(PaymentRecord)
            .filter_by(order_id=entity.get("order_id"))
            .with_for_update()
            .first()
        )
        if (
            not payment
            or entity.get("amount") != payment.amount_paise
            or entity.get("currency") != "INR"
        ):
            raise HTTPException(409, "Unknown order or mismatched amount")
        existing_event = db.get(ProviderEvent, event_id)
        if existing_event:
            if existing_event.payload_hash != hashlib.sha256(raw).hexdigest():
                raise HTTPException(409, "Event ID is already bound to different data")
            return {"status": "duplicate"}
        payment.state = "captured"
        payment.updated_at = processing_time(db)
        db.query(DecisionRecord).filter_by(
            request_id=payment.request_id
        ).one().razorpay_status = "captured"
        db.add(
            ProviderEvent(
                event_id=event_id, payload_hash=hashlib.sha256(raw).hexdigest()
            )
        )
        db.commit()
        return {"status": "recorded"}


app.include_router(secured)
from app.api.risk import router as risk_router

app.include_router(risk_router)
from app.api.authority import router as authority_router

app.include_router(authority_router)
from app.api.merchants import router as merchant_router

app.include_router(merchant_router)
from app.api.operations import router as operations_router

app.include_router(operations_router)
from app.api.providers import router as providers_router

app.include_router(providers_router)


@app.get(
    "/feedback/cases",
    dependencies=[Depends(require_api_key), Depends(enforce_rate_limit)],
)
def feedback_cases(
    db: Session = Depends(get_session), principal: Principal = Depends(require_api_key)
):
    principal.require("admin", "owner", "reviewer")
    q = (
        db.query(DecisionRecord)
        .join(TransactionRecord)
        .options(joinedload(DecisionRecord.transaction))
        .filter(DecisionRecord.human_override.isnot(None))
    )
    q = scoped(q, principal, db)
    rows = q.order_by(DecisionRecord.id.desc()).limit(200).all()
    from app.eval.feedback_export import export_case

    return [export_case(db, d) for d in rows]


@app.get(
    "/metrics", dependencies=[Depends(require_api_key), Depends(enforce_rate_limit)]
)
def metrics(
    db: Session = Depends(get_session), principal: Principal = Depends(require_api_key)
):
    principal.require("admin")
    states = dict(
        db.query(PaymentRecord.state, func.count()).group_by(PaymentRecord.state).all()
    )
    decisions = (
        db.query(DecisionRecord).order_by(DecisionRecord.id.desc()).limit(1000).all()
    )

    def distribution(rows):
        counts = {
            v: sum(d.verdict == v for d in rows) for v in ("APPROVE", "HOLD", "BLOCK")
        }
        return {k: v / len(rows) if rows else 0 for k, v in counts.items()}

    cut = min(500, len(decisions) // 2)
    recent, previous = (
        distribution(decisions[:cut]),
        distribution(decisions[cut : cut * 2]),
    )
    distance = sum(abs(recent[k] - previous[k]) for k in recent) / 2
    tokens = sum((d.token_usage or {}).get("input_tokens", 0) or 0 for d in decisions)
    known_costs = [
        (d.token_usage or {}).get("cost_usd")
        for d in decisions
        if (d.token_usage or {}).get("cost_usd") is not None
    ]
    return {
        "payments": states,
        "queue": eval_queue.stats(),
        "review_backlog": db.query(DecisionRecord)
        .filter_by(verdict="HOLD", human_override=None)
        .count(),
        "observed_input_tokens": tokens,
        "reported_cost_usd": sum(known_costs) if known_costs else None,
        "distribution_monitor": {
            "samples_per_window": cut,
            "recent": recent,
            "previous": previous,
            "total_variation": distance,
            "alert": cut >= 20 and distance > 0.2,
            "note": "Descriptive, uncalibrated signal; never changes payment authorization",
        },
    }

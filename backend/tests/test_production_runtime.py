"""Failure and concurrency regressions at production trust boundaries."""

import asyncio
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
import json
import threading
from unittest.mock import MagicMock
import uuid

from fastapi.testclient import TestClient
import pytest
from sqlalchemy.exc import OperationalError
from app.api.main import app
from app.core.http_limits import RequestLimitsMiddleware
from app.core.orchestrator import SentinelOrchestrator, ConflictError
from app.core.payment_recovery import recover_stale_dispatches
from app.core.postgres_queue import PostgresEvaluationQueue
from app.core.queue_errors import QueueCapacityError
from app.core.settings import database_options
from app.models.db import (
    Base,
    engine,
    SessionLocal,
    PaymentRecord,
    DecisionRecord,
    PolicyRecord,
    DispatchGuard,
    DispatchClaim,
    EvaluationJob,
)
from app.models.policy import Decision
from test_intelligence import make_policy, request, pending


@pytest.fixture
def db():
    Base.metadata.create_all(engine)
    with SessionLocal() as session:
        session.query(EvaluationJob).delete()
        session.query(DispatchGuard).delete()
        session.commit()
        yield session
        session.rollback()
        session.query(EvaluationJob).delete()
        session.query(DispatchGuard).delete()
        session.commit()


def test_semantic_call_releases_transaction_and_rechecks_budget(db):
    p = make_policy(db)
    p.max_daily_total = 1000
    row = db.query(PolicyRecord).filter_by(policy_id=p.policy_id).one()
    row.policy_json = p.to_dict()
    db.commit()
    req = request(p, amount=700, stated_intent="groceries for meals")
    o = SentinelOrchestrator(db)

    def model(policy, incoming):
        assert not db.in_transaction()
        with SessionLocal() as other:
            assert (
                SentinelOrchestrator(other)
                .evaluate_transaction(p, request(p, amount=700))
                .verdict
                == "APPROVE"
            )
        return Decision(
            incoming.request_id,
            "APPROVE",
            ["Fake model for race test"],
            [],
            "llm_reasoner",
        )

    o.reasoner.evaluate = model
    decision = o.evaluate_transaction(p, req)
    assert decision.verdict == "BLOCK"
    assert db.get(PaymentRecord, req.request_id) is None


def test_semantic_result_is_held_when_policy_changes(db):
    p = make_policy(db)
    req = request(p, stated_intent="groceries for meals")
    o = SentinelOrchestrator(db)

    def model(policy, incoming):
        assert not db.in_transaction()
        with SessionLocal() as other:
            row = other.query(PolicyRecord).filter_by(policy_id=p.policy_id).one()
            document = dict(row.policy_json)
            document["version"] = 2
            row.policy_json = document
            other.commit()
        return Decision(
            incoming.request_id, "APPROVE", ["Fake model"], [], "llm_reasoner"
        )

    o.reasoner.evaluate = model
    decision = o.evaluate_transaction(p, req)
    assert (
        decision.verdict == "HOLD"
        and "semantic_policy_changed" in decision.checked_rules
    )


def test_slow_semantic_work_does_not_lock_out_fast_same_agent_request(db):
    p = make_policy(db)
    entered, release = threading.Event(), threading.Event()

    def slow():
        with SessionLocal() as local:
            o = SentinelOrchestrator(local)

            def model(policy, req):
                entered.set()
                assert release.wait(3)
                return Decision(
                    req.request_id, "HOLD", ["Fake model"], [], "llm_reasoner"
                )

            o.reasoner.evaluate = model
            return o.evaluate_transaction(
                p, request(p, stated_intent="groceries for meals")
            )

    with ThreadPoolExecutor(max_workers=2) as pool:
        future = pool.submit(slow)
        assert entered.wait(2)
        try:
            fast = pool.submit(lambda: fast_request(p))
            assert fast.result(timeout=2) == "APPROVE"
        finally:
            release.set()
        assert future.result(timeout=2).verdict == "HOLD"


def fast_request(p):
    with SessionLocal() as local:
        return SentinelOrchestrator(local).evaluate_transaction(p, request(p)).verdict


def test_expired_pending_approval_cannot_dispatch(db):
    p = make_policy(db)
    req, o = pending(db, p)
    payment = db.get(PaymentRecord, req.request_id)
    payment.dispatch_expires_at = datetime.now(timezone.utc) - timedelta(seconds=1)
    db.commit()
    assert (
        SentinelOrchestrator.dispatch_payment(o, req.request_id)["status"] == "expired"
    )
    o.gateway.create_order.assert_not_called()
    assert db.get(PaymentRecord, req.request_id).state == "expired"


def test_deployment_pause_holds_payment_even_with_open_database_guard(db, monkeypatch):
    p = make_policy(db)
    req, o = pending(db, p)
    monkeypatch.setenv("SENTINEL_DISPATCH_PAUSED", "1")

    result = SentinelOrchestrator.dispatch_payment(o, req.request_id)

    assert result["status"] == "guard_hold"
    assert "deployment" in result["reason"]
    assert db.get(PaymentRecord, req.request_id).state == "pending"
    assert db.get(DispatchClaim, req.request_id) is None
    o.gateway.create_order.assert_not_called()


def test_crashed_dispatch_stays_reserved_as_unknown(db):
    p = make_policy(db)
    req, o = pending(db, p)
    payment = db.get(PaymentRecord, req.request_id)
    payment.state = "processing"
    payment.updated_at = datetime.now(timezone.utc) - timedelta(minutes=3)
    db.commit()
    assert recover_stale_dispatches(db) == 1
    assert (
        SentinelOrchestrator.dispatch_payment(o, req.request_id)["status"] == "unknown"
    )
    o.gateway.create_order.assert_not_called()
    assert any(
        h.counts_against_budget
        for h in o.audit.get_recent_transactions_for_agent(p.agent_id)
    )


def test_late_provider_response_cannot_downgrade_capture(db):
    p = make_policy(db)
    req, o = pending(db, p)

    # Retain the order ID before the context closes/ORM state expires.
    def captured(*args, **kwargs):
        order_id = "order_capture_" + uuid.uuid4().hex
        with SessionLocal() as other:
            payment = other.get(PaymentRecord, req.request_id)
            payment.state, payment.order_id = "captured", order_id
            other.query(DecisionRecord).filter_by(
                request_id=req.request_id
            ).one().razorpay_status = "captured"
            other.commit()
        return {"status": "created", "order_id": order_id}

    o.gateway.create_order = captured
    assert (
        SentinelOrchestrator.dispatch_payment(o, req.request_id)["status"] == "captured"
    )
    assert (
        db.query(PaymentRecord)
        .filter_by(request_id=req.request_id)
        .populate_existing()
        .one()
        .state
        == "captured"
    )


def test_pending_cancellation_is_scoped_and_never_cancels_unknown(db):
    p = make_policy(db)
    req, o = pending(db, p)
    c = TestClient(app)
    headers = {"X-API-Key": "test-key-contracts"}
    assert (
        c.post(
            "/payments/" + req.request_id + "/cancel",
            headers=headers,
            json={"reason": "Do not resume"},
        ).json()["status"]
        == "cancelled"
    )
    assert (
        SentinelOrchestrator.dispatch_payment(o, req.request_id)["status"]
        == "cancelled"
    )
    assert (
        c.post(
            "/payments/" + req.request_id + "/cancel",
            headers=headers,
            json={"reason": "Again"},
        ).status_code
        == 409
    )
    o.gateway.create_order.assert_not_called()


def test_credential_revocation_stops_previously_approved_pending_payment(
    db, monkeypatch
):
    p = make_policy(db)
    o = SentinelOrchestrator(db)
    o.dispatch_payment = MagicMock()
    req = request(p)
    o.evaluate_transaction(p, req, actor_id="local-admin")
    monkeypatch.setenv(
        "SENTINEL_CREDENTIALS",
        json.dumps([{"key": "x" * 40, "subject": "different-admin", "role": "admin"}]),
    )
    assert (
        SentinelOrchestrator.dispatch_payment(o, req.request_id)["status"]
        == "guard_hold"
    )


def test_sql_queue_atomic_capacity_and_restart_persistence(db):
    async def run():
        q = PostgresEvaluationQueue(lambda p: p, max_queue_size=2)
        ids = await q.enqueue_batch(
            [{"agent_id": "a", "value": 1}, {"agent_id": "a", "value": 2}], "batch"
        )
        with pytest.raises(QueueCapacityError):
            await q.enqueue_batch([{"agent_id": "a"}], "rejected")
        recovered = PostgresEvaluationQueue(
            lambda p: {"value": p["value"]}, num_workers=1
        )
        assert recovered.get_batch("rejected") is None
        await recovered.start()
        try:
            for _ in range(100):
                batch = recovered.get_batch("batch")
                if batch["complete"]:
                    break
                await asyncio.sleep(0.02)
            assert batch["by_status"]["DONE"] == 2
            assert all(
                recovered.get_job(jid)["result"]["value"] in (1, 2) for jid in ids
            )
        finally:
            await recovered.stop()

    asyncio.run(run())


def test_sql_lease_fences_stale_worker_completion(db):
    q = PostgresEvaluationQueue(lambda p: p)
    jid = asyncio.run(q.enqueue({"agent_id": "a"}))
    first = q._claim()
    row = db.get(EvaluationJob, jid)
    row.lease_expires_at = datetime.now(timezone.utc) - timedelta(seconds=1)
    db.commit()
    second = PostgresEvaluationQueue(lambda p: p)._claim()
    assert first[0] == second[0] and first[1] != second[1]
    assert not q._finish(first[0], first[1], result={"stale": True})
    assert q._finish(second[0], second[1], result={"current": True})
    assert q.get_job(jid)["result"] == {"current": True}


def test_expired_sql_lease_cannot_be_resurrected_or_finished_before_takeover(db):
    q = PostgresEvaluationQueue(lambda p: p)
    jid = asyncio.run(q.enqueue({"agent_id": "a"}))
    _, old_token, _ = q._claim()
    db.get(EvaluationJob, jid).lease_expires_at = datetime.now(
        timezone.utc
    ) - timedelta(seconds=1)
    db.commit()
    assert not q._renew(jid, old_token)
    assert not q._finish(jid, old_token, result={"late": True})
    assert q.get_job(jid)["status"] == "PROCESSING"
    _, winner, _ = q._claim()
    assert winner != old_token
    assert q._renew(jid, winner)
    assert q._finish(jid, winner, result={"recovered": True})


def test_job_admission_timestamps_use_database_clock(db, monkeypatch):
    trusted = datetime.now(timezone.utc) - timedelta(days=3)
    monkeypatch.setattr("app.core.postgres_queue.processing_time", lambda db: trusted)
    q = PostgresEvaluationQueue(lambda p: p)
    jid = asyncio.run(q.enqueue({"agent_id": "a"}))
    row = db.get(EvaluationJob, jid)
    assert row.created_at.replace(tzinfo=timezone.utc) == trusted
    assert row.updated_at.replace(tzinfo=timezone.utc) == trusted


def test_retried_sql_commands_do_not_escape_submission_deadline(db):
    q = PostgresEvaluationQueue(lambda p: p)
    jid = asyncio.run(q.enqueue({"agent_id": "a"}))
    assert q._claim()
    row = db.get(EvaluationJob, jid)
    row.created_at = datetime.now(timezone.utc) - timedelta(seconds=301)
    row.lease_expires_at = datetime.now(timezone.utc) - timedelta(seconds=1)
    db.commit()
    assert q._claim() is None
    assert q.get_job(jid)["status"] == "FAILED"
    assert "expired" in q.get_job(jid)["error"]


def test_live_worker_lease_does_not_extend_command_authorization_deadline(db):
    p = make_policy(db)
    req = request(p)
    q = PostgresEvaluationQueue(lambda p: p)
    jid = asyncio.run(q.enqueue({"agent_id": p.agent_id}))
    _, token, _ = q._claim()
    row = db.get(EvaluationJob, jid)
    row.created_at = datetime.now(timezone.utc) - timedelta(seconds=301)
    db.commit()
    with pytest.raises(ConflictError, match="command expired"):
        SentinelOrchestrator(db).evaluate_transaction(
            p, req, work_lease=dict(job_id=jid, token=token)
        )
    assert db.get(PaymentRecord, req.request_id) is None


def test_sql_claims_are_unique_across_workers(db):
    q = PostgresEvaluationQueue(lambda p: p)
    ids = asyncio.run(q.enqueue_batch([{"agent_id": "a"} for _ in range(4)]))
    with ThreadPoolExecutor(max_workers=4) as pool:
        claims = list(pool.map(lambda _: q._claim(), range(4)))
    assert {item[0] for item in claims} == set(ids)


def test_sql_transient_failure_retries_stable_request(db):
    calls = []

    def process(payload):
        calls.append(payload["request_id"])
        if len(calls) == 1:
            raise OperationalError("select 1", None, ConnectionError("temporary"))
        return {"ok": True}

    async def run():
        q = PostgresEvaluationQueue(process, num_workers=1)
        jid = await q.enqueue({"agent_id": "a"})
        await q.start()
        try:
            for _ in range(150):
                job = q.get_job(jid)
                if job["status"] == "DONE":
                    break
                await asyncio.sleep(0.02)
            assert job["status"] == "DONE" and len(calls) == 2 and calls[0] == calls[1]
        finally:
            await q.stop()

    asyncio.run(run())


def test_sql_retention_keeps_active_jobs_and_whole_unfinished_batches(db):
    q = PostgresEvaluationQueue(lambda p: p)
    ids = asyncio.run(
        q.enqueue_batch([{"agent_id": "a"}, {"agent_id": "a"}], "unfinished")
    )
    row = db.get(EvaluationJob, ids[0])
    row.status = "DONE"
    row.completed_at = datetime.now(timezone.utc) - timedelta(days=2)
    db.commit()
    q._cleanup()
    assert q.get_batch("unfinished")["total"] == 2
    row = db.get(EvaluationJob, ids[1])
    row.status = "DONE"
    row.completed_at = datetime.now(timezone.utc) - timedelta(days=2)
    db.commit()
    q._cleanup()
    assert q.get_batch("unfinished") is None


def test_body_limit_applies_without_content_length(monkeypatch):
    monkeypatch.setenv("SENTINEL_MAX_BODY_BYTES", "65536")
    reached = []
    sent = []

    async def downstream(scope, receive, send):
        reached.append(True)

    middleware = RequestLimitsMiddleware(downstream)
    chunks = iter(
        [
            {"type": "http.request", "body": b"x" * 40000, "more_body": True},
            {"type": "http.request", "body": b"x" * 40000},
        ]
    )

    async def receive():
        return next(chunks)

    async def send(message):
        sent.append(message)

    asyncio.run(
        middleware(
            {"type": "http", "path": "/transactions/evaluate", "headers": []},
            receive,
            send,
        )
    )
    assert sent[0]["status"] == 413 and not reached and middleware.active == 0


def test_body_and_trace_limits_on_http_requests():
    c = TestClient(app)
    assert c.post("/auth/login", content=b"x" * 600000).status_code == 413
    r = c.get("/health/live", headers={"X-Request-ID": "x" * 500})
    assert r.status_code == 200 and len(r.headers["X-Request-ID"]) < 129


def test_database_options_preserve_schema_and_bound_pool():
    options = database_options(
        "postgresql://u:p@localhost/example_test?options=-csearch_path%3Disolated"
    )
    assert "-csearch_path=isolated" in options["connect_args"]["options"]
    assert "timezone=UTC" in options["connect_args"]["options"]
    assert options["max_overflow"] == 0 and options["pool_timeout"] == 3


def test_exhausted_sql_job_is_terminal_instead_of_a_poison_loop(db):
    q = PostgresEvaluationQueue(lambda p: p)
    jid = asyncio.run(q.enqueue({"agent_id": "a"}))
    for _ in range(5):
        assert q._claim()[0] == jid
        row = db.get(EvaluationJob, jid)
        row.lease_expires_at = datetime.now(timezone.utc) - timedelta(seconds=1)
        db.commit()
    assert q._claim() is None
    assert q.get_job(jid)["status"] == "FAILED"


def test_graceful_queue_shutdown_finishes_current_work(db):
    entered, release = threading.Event(), threading.Event()

    def work(payload):
        entered.set()
        assert release.wait(3)
        return {"finished": True}

    async def run():
        q = PostgresEvaluationQueue(work, num_workers=1)
        jid = await q.enqueue({"agent_id": "a"})
        await q.start()
        assert await asyncio.to_thread(entered.wait, 2)
        shutdown = asyncio.create_task(q.stop())
        await asyncio.sleep(0.05)
        release.set()
        await shutdown
        assert q.get_job(jid)["status"] == "DONE"
        with pytest.raises(QueueCapacityError):
            await q.enqueue({"agent_id": "a"})

    asyncio.run(run())


def test_policy_replacement_stops_pending_dispatch(db):
    p = make_policy(db)
    o = SentinelOrchestrator(db)
    o.dispatch_payment = MagicMock()
    req = request(p)
    o.evaluate_transaction(p, req, actor_id="local-admin")
    row = db.query(PolicyRecord).filter_by(policy_id=p.policy_id).one()
    row.active = False
    db.commit()
    assert (
        SentinelOrchestrator.dispatch_payment(o, req.request_id)["status"]
        == "guard_hold"
    )


def test_emergency_freeze_does_not_depend_on_redis(db):
    from app.ops.freeze import freeze
    from app.models.db import RiskControlEvent

    result = freeze(db, "Redis outage investigation")
    assert result["frozen"] and result["version"] == 1
    assert db.query(RiskControlEvent).filter_by(kind="emergency_freeze").count() >= 1


def test_unknown_provider_result_is_not_recorded_as_created(db):
    p = make_policy(db)
    req, o = pending(db, p)
    o.gateway.create_order = MagicMock(
        return_value={"status": "created", "order_id": None}
    )
    assert (
        SentinelOrchestrator.dispatch_payment(o, req.request_id)["status"] == "unknown"
    )


def test_live_llm_capacity_fails_to_hold_without_provider_call(monkeypatch):
    import app.core.llm_client as module

    monkeypatch.setattr(module, "_LIVE_SLOTS", threading.BoundedSemaphore(0))
    client = module.LLMClient()
    client.mock_mode = False
    client._client = MagicMock()
    assert (
        client.complete_json("system", "user", {})["error"]
        == "llm_capacity_unavailable"
    )
    client._client.chat.completions.create.assert_not_called()


def test_production_schema_requires_caller_idempotency_key(monkeypatch):
    from app.core.schemas import TransactionIn

    monkeypatch.setenv("SENTINEL_ENV", "production")
    with pytest.raises(ValueError):
        TransactionIn(
            agent_id="a",
            merchant_name="m",
            merchant_category="grocery",
            amount=1,
            stated_intent="buy",
        )


def test_same_subject_cannot_read_job_after_scope_changes(db, monkeypatch):
    from app.api import main

    q = PostgresEvaluationQueue(lambda p: p)
    monkeypatch.setattr(main, "eval_queue", q)
    p = make_policy(db)
    key = "o" * 40
    monkeypatch.setenv(
        "SENTINEL_CREDENTIALS",
        json.dumps(
            [
                {
                    "key": key,
                    "subject": "same-person",
                    "role": "owner",
                    "owner_id": p.owner_human_id,
                }
            ]
        ),
    )
    jid = asyncio.run(
        q.enqueue({"agent_id": p.agent_id, "_submitted_by": "same-person"})
    )
    c = TestClient(app)
    assert c.get("/jobs/" + jid, headers={"X-API-Key": key}).status_code == 200
    monkeypatch.setenv(
        "SENTINEL_CREDENTIALS",
        json.dumps(
            [
                {
                    "key": key,
                    "subject": "same-person",
                    "role": "agent",
                    "agent_ids": ["different"],
                }
            ]
        ),
    )
    assert c.get("/jobs/" + jid, headers={"X-API-Key": key}).status_code == 403


def test_lost_job_lease_cannot_authorize_even_before_completion(db):
    from app.core.orchestrator import ConflictError
    from app.models.db import TransactionRecord

    p = make_policy(db)
    req = request(p)
    q = PostgresEvaluationQueue(lambda p: p)
    jid = asyncio.run(q.enqueue(dict(req.__dict__, _submitted_by="local-admin")))
    _, old_token, _ = q._claim()
    row = db.get(EvaluationJob, jid)
    row.lease_expires_at = datetime.now(timezone.utc) - timedelta(seconds=1)
    db.commit()
    _, new_token, _ = q._claim()
    with pytest.raises(ConflictError):
        SentinelOrchestrator(db).evaluate_transaction(
            p,
            req,
            actor_id="local-admin",
            work_lease={"job_id": jid, "token": old_token},
        )
    assert db.query(TransactionRecord).filter_by(request_id=req.request_id).count() == 0
    assert (
        SentinelOrchestrator(db)
        .evaluate_transaction(
            p,
            req,
            actor_id="local-admin",
            work_lease={"job_id": jid, "token": new_token},
        )
        .verdict
        == "APPROVE"
    )


def test_untouched_expired_job_does_not_authorize_after_long_outage(db):
    q = PostgresEvaluationQueue(lambda p: p)
    jid = asyncio.run(q.enqueue({"agent_id": "a"}))
    row = db.get(EvaluationJob, jid)
    row.created_at = datetime.now(timezone.utc) - timedelta(minutes=10)
    db.commit()
    assert q._claim() is None
    assert q.get_job(jid)["status"] == "FAILED"


def test_non_ascii_credential_is_rejected_without_internal_error():
    from app.core.auth import authenticate
    from fastapi import HTTPException

    with pytest.raises(HTTPException) as error:
        authenticate("é" * 40)
    assert error.value.status_code == 401

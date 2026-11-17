"""Authoritative SQL job queue: atomic admission, leased claims and fenced completion.

PostgreSQL SKIP LOCKED permits independent workers to claim different jobs. SQLite
uses the existing local lock for isolated tests only. Delivery remains at least
once; request IDs and payment claims remain the authority for side effects.
"""

import asyncio
from datetime import timedelta
import logging
import uuid
from sqlalchemy import and_, or_, update
from sqlalchemy.exc import OperationalError, TimeoutError as DBTimeout
from app.models.db import SessionLocal, EvaluationJob, BrowserSession
from app.core.intelligence import processing_time
from app.core.locking import agent_transaction
from app.core.queue_errors import QueueCapacityError
from app.core.settings import integer

logger = logging.getLogger("sentinel.durable_queue")
TERMINAL = ("DONE", "FAILED")


def job_view(row):
    return {
        "job_id": row.job_id,
        "status": row.status,
        "result": row.result,
        "error": row.error,
        "submitted_by": row.submitted_by,
        "agent_id": row.agent_id,
    }


class PostgresEvaluationQueue:
    def __init__(self, submit_fn, num_workers=4, max_queue_size=500, lease_seconds=60):
        self._submit_fn, self._num_workers = submit_fn, num_workers
        self._capacity, self._lease_seconds = max_queue_size, lease_seconds
        self._workers = []
        self._stopping = False
        self._maintenance = None

    async def start(self):
        self._stopping = False
        self._workers = [
            asyncio.create_task(self._worker_loop()) for _ in range(self._num_workers)
        ]
        self._maintenance = asyncio.create_task(self._maintenance_loop())

    async def stop(self):
        self._stopping = True
        if self._maintenance:
            self._maintenance.cancel()
            await asyncio.gather(self._maintenance, return_exceptions=True)
        if self._workers:
            _, pending = await asyncio.wait(
                self._workers,
                timeout=integer("SENTINEL_SHUTDOWN_SECONDS", 30, maximum=60),
            )
            for task in pending:
                task.cancel()
            await asyncio.gather(*self._workers, return_exceptions=True)
        self._workers = []

    def _enqueue(self, payloads, batch_id):
        if self._stopping:
            raise QueueCapacityError("Queue is draining")
        if not 1 <= len(payloads) <= 100:
            raise ValueError("batch size must be 1..100")
        db = SessionLocal()
        try:
            with agent_transaction(db, "__sentinel_job_admission_v1__"):
                count = (
                    db.query(EvaluationJob)
                    .filter(EvaluationJob.status.in_(("QUEUED", "PROCESSING")))
                    .count()
                )
                if count + len(payloads) > self._capacity:
                    raise QueueCapacityError(
                        "Queue capacity exceeded; no jobs accepted"
                    )
                ids = []
                for original in payloads:
                    payload = dict(original)
                    payload.setdefault("request_id", "req_" + uuid.uuid4().hex)
                    jid = "job_" + uuid.uuid4().hex
                    db.add(
                        EvaluationJob(
                            job_id=jid,
                            batch_id=batch_id,
                            payload=payload,
                            submitted_by=payload.get("_submitted_by", ""),
                            agent_id=payload.get("agent_id", ""),
                        )
                    )
                    ids.append(jid)
            return ids
        finally:
            db.close()

    async def enqueue_batch(self, payloads, batch_id=None):
        return await asyncio.to_thread(self._enqueue, payloads, batch_id)

    async def enqueue(self, payload, batch_id=None):
        return (await self.enqueue_batch([payload], batch_id))[0]

    def get_job(self, job_id):
        with SessionLocal() as db:
            row = db.get(EvaluationJob, job_id)
            return job_view(row) if row else None

    def get_batch(self, batch_id):
        with SessionLocal() as db:
            rows = (
                db.query(EvaluationJob)
                .filter_by(batch_id=batch_id)
                .order_by(EvaluationJob.created_at, EvaluationJob.job_id)
                .limit(100)
                .all()
            )
            if not rows:
                return None
            jobs = [job_view(r) for r in rows]
        return {
            "batch_id": batch_id,
            "total": len(jobs),
            "by_status": {
                s: sum(j["status"] == s for j in jobs)
                for s in ("QUEUED", "PROCESSING", "DONE", "FAILED")
            },
            "complete": all(j["status"] in TERMINAL for j in jobs),
            "job_ids": [j["job_id"] for j in jobs],
            "jobs": jobs,
        }

    def stats(self):
        from sqlalchemy import func

        with SessionLocal() as db:
            counts = dict(
                db.query(EvaluationJob.status, func.count())
                .filter(EvaluationJob.status.in_(("QUEUED", "PROCESSING")))
                .group_by(EvaluationJob.status)
                .all()
            )
        return {
            "backend": "postgres",
            "queue_depth": counts.get("QUEUED", 0) + counts.get("PROCESSING", 0),
            "by_status": counts,
            "live_workers": sum(not t.done() for t in self._workers),
        }

    def _claim(self):
        with SessionLocal() as db:
            with (
                agent_transaction(db, "__sentinel_job_claim_v1__")
                if db.bind.dialect.name == "sqlite"
                else db.begin()
            ):
                now = processing_time(db)
                row = (
                    db.query(EvaluationJob)
                    .filter(
                        or_(
                            EvaluationJob.status == "QUEUED",
                            and_(
                                EvaluationJob.status == "PROCESSING",
                                EvaluationJob.lease_expires_at < now,
                            ),
                        )
                    )
                    .order_by(EvaluationJob.created_at, EvaluationJob.job_id)
                    .with_for_update(skip_locked=True)
                    .first()
                )
                if row is None:
                    return None
                if row.attempts == 0 and now - row.created_at.replace(
                    tzinfo=now.tzinfo
                ) > timedelta(
                    seconds=integer(
                        "SENTINEL_JOB_MAX_AGE_SECONDS", 300, minimum=30, maximum=3600
                    )
                ):
                    row.status, row.error, row.completed_at = (
                        "FAILED",
                        "Job expired before authorization; submit a fresh request",
                        now,
                    )
                    return None
                row.attempts += 1
                if row.attempts > 5:
                    row.status, row.error, row.completed_at = (
                        "FAILED",
                        "Retry budget exhausted; inspect durable request state",
                        now,
                    )
                    return None
                row.status, row.lease_token = "PROCESSING", uuid.uuid4().hex
                row.lease_expires_at = now + timedelta(seconds=self._lease_seconds)
                return row.job_id, row.lease_token, dict(row.payload)

    def _renew(self, jid, token):
        with SessionLocal() as db:
            now = processing_time(db)
            changed = db.execute(
                update(EvaluationJob)
                .where(
                    EvaluationJob.job_id == jid,
                    EvaluationJob.lease_token == token,
                    EvaluationJob.status == "PROCESSING",
                )
                .values(lease_expires_at=now + timedelta(seconds=self._lease_seconds))
            ).rowcount
            db.commit()
            return changed == 1

    def _finish(self, jid, token, *, result=None, error=None, retry=False):
        with SessionLocal() as db:
            now = processing_time(db)
            changed = db.execute(
                update(EvaluationJob)
                .where(
                    EvaluationJob.job_id == jid,
                    EvaluationJob.lease_token == token,
                    EvaluationJob.status == "PROCESSING",
                )
                .values(
                    status="QUEUED" if retry else "FAILED" if error else "DONE",
                    result=result,
                    error=error,
                    completed_at=None if retry else now,
                    lease_token=None,
                    lease_expires_at=None,
                )
            ).rowcount
            db.commit()
            return changed == 1

    def _cleanup(self):
        from sqlalchemy import func

        with SessionLocal() as db:
            cutoff = processing_time(db) - timedelta(
                hours=integer("SENTINEL_JOB_RETENTION_HOURS", 24, maximum=720)
            )
            batches = [
                r[0]
                for r in db.query(EvaluationJob.batch_id)
                .filter(EvaluationJob.batch_id.isnot(None))
                .group_by(EvaluationJob.batch_id)
                .having(
                    func.count(EvaluationJob.completed_at)
                    == func.count(EvaluationJob.job_id),
                    func.max(EvaluationJob.completed_at) < cutoff,
                )
                .limit(10)
                .all()
            ]
            if batches:
                db.query(EvaluationJob).filter(
                    EvaluationJob.batch_id.in_(batches)
                ).delete(synchronize_session=False)
            singles = [
                r[0]
                for r in db.query(EvaluationJob.job_id)
                .filter(
                    EvaluationJob.batch_id.is_(None),
                    EvaluationJob.status.in_(TERMINAL),
                    EvaluationJob.completed_at < cutoff,
                )
                .limit(1000)
            ]
            if singles:
                db.query(EvaluationJob).filter(
                    EvaluationJob.job_id.in_(singles)
                ).delete(synchronize_session=False)
            expired = [
                r[0]
                for r in db.query(BrowserSession.token_hash)
                .filter(BrowserSession.expires_at < processing_time(db))
                .limit(1000)
            ]
            if expired:
                db.query(BrowserSession).filter(
                    BrowserSession.token_hash.in_(expired)
                ).delete(synchronize_session=False)
            db.commit()

    async def _maintenance_loop(self):
        while not self._stopping:
            try:
                await asyncio.to_thread(self._cleanup)
            except Exception:
                logger.warning("Queue maintenance unavailable")
            await asyncio.sleep(60)

    async def _heartbeat(self, jid, token):
        while True:
            await asyncio.sleep(max(0.05, self._lease_seconds / 3))
            if not await asyncio.to_thread(self._renew, jid, token):
                return

    async def _worker_loop(self):
        while not self._stopping:
            try:
                claimed = await asyncio.to_thread(self._claim)
                if claimed is None:
                    await asyncio.sleep(0.25)
                    continue
                jid, token, payload = claimed
                heartbeat = asyncio.create_task(self._heartbeat(jid, token))
                try:
                    payload["_job_lease"] = {"job_id": jid, "token": token}
                    result = await asyncio.to_thread(self._submit_fn, payload)
                    await asyncio.to_thread(self._finish, jid, token, result=result)
                except (OperationalError, DBTimeout):
                    # A lost DB connection is not an authorization rejection. Keep
                    # the same request and let lease recovery retry safely.
                    await asyncio.to_thread(
                        self._finish,
                        jid,
                        token,
                        error="Database temporarily unavailable",
                        retry=True,
                    )
                    await asyncio.sleep(1)
                except asyncio.CancelledError:
                    raise  # Keep the claim for lease-based recovery.
                except Exception:
                    logger.exception("Durable evaluation failed")
                    await asyncio.to_thread(
                        self._finish,
                        jid,
                        token,
                        error="Evaluation failed; inspect server logs",
                    )
                finally:
                    heartbeat.cancel()
                    await asyncio.gather(heartbeat, return_exceptions=True)
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception(
                    "Durable queue dependency unavailable; claims remain recoverable"
                )
                await asyncio.sleep(1)

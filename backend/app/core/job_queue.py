"""Bounded development queue. Use Redis Streams for shared durable delivery."""

from __future__ import annotations
import asyncio
import uuid
import logging
import time
from dataclasses import dataclass
from enum import Enum

logger = logging.getLogger("sentinel.job_queue")


class JobStatus(str, Enum):
    QUEUED = "QUEUED"
    PROCESSING = "PROCESSING"
    DONE = "DONE"
    FAILED = "FAILED"


@dataclass
class Job:
    job_id: str
    status: JobStatus = JobStatus.QUEUED
    result: dict | None = None
    error: str | None = None
    submitted_by: str = ""
    agent_id: str = ""
    completed_at: float | None = None


class EvaluationQueue:
    """
    Holds pending evaluation jobs and runs them via a bounded pool of
    async workers. `submit_fn` is the actual evaluation callable (sync,
    run in a thread to avoid blocking the event loop, since the
    underlying DB/rule-engine work is synchronous).
    """

    def __init__(self, submit_fn, num_workers: int = 4, max_queue_size: int = 500):
        self._queue: asyncio.Queue = asyncio.Queue(maxsize=max_queue_size)
        self._jobs: dict[str, Job] = {}
        self._batches: dict[str, list[str]] = {}
        self._submit_fn = submit_fn
        self._num_workers = num_workers
        self._workers: list[asyncio.Task] = []
        self._lock = asyncio.Lock()

    async def start(self):
        self._queue = asyncio.Queue(maxsize=self._queue.maxsize)
        self._lock = asyncio.Lock()
        self._workers = [
            asyncio.create_task(self._worker_loop(i)) for i in range(self._num_workers)
        ]
        logger.info(f"EvaluationQueue started with {self._num_workers} workers.")

    async def stop(self):
        for w in self._workers:
            w.cancel()
        await asyncio.gather(*self._workers, return_exceptions=True)
        self._workers = []
        for job in self._jobs.values():
            if job.status not in (JobStatus.DONE, JobStatus.FAILED):
                job.status = JobStatus.FAILED
                job.error = "Development queue stopped; inspect durable request state before retrying"
                job.completed_at = time.time()

    async def enqueue(self, payload: dict, batch_id: str | None = None) -> str:
        job_id = f"job_{uuid.uuid4().hex[:10]}"
        payload = dict(payload)
        payload.setdefault("request_id", "req_" + uuid.uuid4().hex)
        job = Job(
            job_id=job_id,
            status=JobStatus.QUEUED,
            submitted_by=payload.get("_submitted_by", ""),
            agent_id=payload.get("agent_id", ""),
        )
        async with self._lock:
            self._jobs[job_id] = job
            if batch_id:
                self._batches.setdefault(batch_id, []).append(job_id)
        await self._queue.put((job_id, payload))
        return job_id

    async def enqueue_batch(self, payloads, batch_id=None):
        if (
            not 1 <= len(payloads) <= 100
            or self._queue.qsize() + len(payloads) > self._queue.maxsize
        ):
            raise ValueError("Invalid batch or queue capacity exceeded")
        return [await self.enqueue(p, batch_id) for p in payloads]

    def _cleanup(self):
        cutoff = time.time() - 3600
        self._jobs = {
            k: v
            for k, v in self._jobs.items()
            if v.completed_at is None or v.completed_at >= cutoff
        }
        self._batches = {
            k: v for k, v in self._batches.items() if any(j in self._jobs for j in v)
        }

    def get_job(self, job_id: str):
        self._cleanup()
        return self._jobs.get(job_id)

    def get_batch(self, batch_id: str) -> dict | None:
        job_ids = self._batches.get(batch_id)
        if job_ids is None:
            return None
        jobs = [self._jobs[jid] for jid in job_ids if jid in self._jobs]
        by_status = {s.value: 0 for s in JobStatus}
        for job in jobs:
            by_status[job.status.value] += 1
        done = by_status["DONE"] + by_status["FAILED"]
        return {
            "batch_id": batch_id,
            "total": len(job_ids),
            "by_status": by_status,
            "complete": done == len(job_ids),
            "job_ids": job_ids,
            "jobs": [
                {
                    "job_id": j.job_id,
                    "status": j.status.value,
                    "result": j.result,
                    "error": j.error,
                    "submitted_by": j.submitted_by,
                    "agent_id": j.agent_id,
                }
                for j in jobs
            ],
        }

    def stats(self) -> dict:
        self._cleanup()
        by_status = {s.value: 0 for s in JobStatus}
        for job in self._jobs.values():
            by_status[job.status.value] += 1
        return {
            "queue_depth": self._queue.qsize(),
            "by_status": by_status,
            "total_jobs": len(self._jobs),
            "backend": "memory",
            "live_workers": sum(not w.done() for w in self._workers),
        }

    async def _worker_loop(self, worker_id: int):
        while True:
            job_id, payload = await self._queue.get()
            job = self._jobs[job_id]
            job.status = JobStatus.PROCESSING
            try:
                # The actual evaluation (rule engine + possible LLM call +
                # DB writes) is synchronous work -- run it in a thread so
                # it can't block other workers or the event loop.
                result = await asyncio.to_thread(self._submit_fn, payload)
                job.result = result
                job.status = JobStatus.DONE
            except Exception:
                logger.exception(f"Job {job_id} failed on worker {worker_id}")
                job.error = "Evaluation failed; inspect server logs"
                job.status = JobStatus.FAILED
            finally:
                job.completed_at = time.time()
                self._queue.task_done()

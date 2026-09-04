"""
Async Evaluation Queue
========================
Decouples "an agent submitted a transaction" from "Sentinel finished
evaluating it" -- a burst of requests (e.g. an agent placing several
purchases in quick succession, or many agents transacting at once) gets
enqueued and processed by a background worker pool, instead of blocking
the caller on each individual rule-engine/LLM-reasoner evaluation.

Design: a single-process asyncio queue with a small worker pool. This is
genuine, correct async concurrency for the workload this system actually
has (a demo/eval-scale burst, not sustained massive throughput). If this
needed to scale beyond one process, the natural next step is a real
broker (Redis Streams / RabbitMQ / SQS) feeding the same worker logic --
documented as a scaling path rather than built now, since a single
process has no multi-instance queue-sharing problem to solve yet.
"""

from __future__ import annotations
import asyncio
import uuid
import logging
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
        self._workers = [asyncio.create_task(self._worker_loop(i)) for i in range(self._num_workers)]
        logger.info(f"EvaluationQueue started with {self._num_workers} workers.")

    async def stop(self):
        for w in self._workers:
            w.cancel()

    async def enqueue(self, payload: dict, batch_id: str | None = None) -> str:
        job_id = f"job_{uuid.uuid4().hex[:10]}"
        job = Job(job_id=job_id, status=JobStatus.QUEUED)
        async with self._lock:
            self._jobs[job_id] = job
            if batch_id:
                self._batches.setdefault(batch_id, []).append(job_id)
        await self._queue.put((job_id, payload))
        return job_id

    def get_job(self, job_id: str):
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
                {"job_id": j.job_id, "status": j.status.value, "result": j.result, "error": j.error}
                for j in jobs
            ],
        }

    def stats(self) -> dict:
        by_status = {s.value: 0 for s in JobStatus}
        for job in self._jobs.values():
            by_status[job.status.value] += 1
        return {"queue_depth": self._queue.qsize(), "by_status": by_status, "total_jobs": len(self._jobs)}

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
            except Exception as e:
                logger.exception(f"Job {job_id} failed on worker {worker_id}")
                job.error = str(e)
                job.status = JobStatus.FAILED
            finally:
                self._queue.task_done()

"""
Redis-Backed Evaluation Queue
===============================
A Redis-backed implementation of the same public interface as
EvaluationQueue (job_queue.py): enqueue / get_job / stats, plus a
worker loop. This is what makes the async queue genuinely usable
across MULTIPLE backend replicas, not just multiple workers within
one process -- job_queue.py's own docstring named this as the natural
next step once real multi-instance scale exists.

Design:
  - Pending jobs live in a Redis LIST (`sentinel:queue:pending`).
    Workers atomically pop from it with BRPOP, which blocks efficiently
    (no busy-polling) and guarantees a job is only ever claimed by one
    worker, even across different backend processes/replicas.
  - Job status/result lives in a Redis HASH per job
    (`sentinel:job:<job_id>`), so any replica's API process can look up
    a job's status regardless of which replica's worker actually
    processed it -- this is the actual problem an in-process dict
    can't solve once there's more than one backend instance.
  - Workers run as asyncio tasks calling the BLOCKING redis client via
    asyncio.to_thread, since redis-py's sync client doesn't have a
    non-blocking BRPOP; this keeps the FastAPI event loop unblocked
    while still getting genuine blocking pop semantics.
"""

from __future__ import annotations
import asyncio
import json
import uuid
import logging

logger = logging.getLogger("sentinel.redis_queue")

try:
    import redis as redis_lib
except ImportError:
    redis_lib = None

QUEUE_KEY = "sentinel:queue:pending"
JOB_KEY_PREFIX = "sentinel:job:"
BATCH_KEY_PREFIX = "sentinel:batch:"
JOB_TTL_SECONDS = 3600  # completed job records expire after an hour


class RedisEvaluationQueue:
    def __init__(
        self,
        submit_fn,
        num_workers: int = 4,
        redis_url: str = "redis://localhost:6379/0",
        queue_key: str = QUEUE_KEY,
    ):
        if redis_lib is None:
            raise RuntimeError("redis package not installed -- pip install redis")
        self._client = redis_lib.Redis.from_url(redis_url, decode_responses=True)
        self._submit_fn = submit_fn
        self._num_workers = num_workers
        self._workers: list = []
        self._stop_flag = False
        self._queue_key = queue_key

    async def start(self):
        self._client.ping()  # fail fast and loud if Redis isn't actually reachable
        self._stop_flag = False
        self._workers = [
            asyncio.create_task(self._worker_loop(i))
            for i in range(self._num_workers)
        ]
        logger.info(
            f"RedisEvaluationQueue started with "
            f"{self._num_workers} workers against {self._queue_key}."
        )

    async def stop(self):
        self._stop_flag = True
        for w in self._workers:
            w.cancel()

    async def enqueue(self, payload: dict, batch_id: str | None = None) -> str:
        job_id = f"job_{uuid.uuid4().hex[:10]}"
        job_key = f"{JOB_KEY_PREFIX}{job_id}"

        def _do_enqueue():
            self._client.hset(
                job_key,
                mapping={"status": "QUEUED", "result": "", "error": ""},
            )
            self._client.expire(job_key, JOB_TTL_SECONDS)
            self._client.lpush(
                self._queue_key,
                json.dumps({"job_id": job_id, "payload": payload}),
            )
            if batch_id:
                batch_key = f"{BATCH_KEY_PREFIX}{batch_id}"
                self._client.rpush(batch_key, job_id)
                self._client.expire(batch_key, JOB_TTL_SECONDS)

        await asyncio.to_thread(_do_enqueue)
        return job_id

    def get_job(self, job_id: str):
        job_key = f"{JOB_KEY_PREFIX}{job_id}"
        data = self._client.hgetall(job_key)
        if not data:
            return None
        result = json.loads(data["result"]) if data.get("result") else None
        return {
            "job_id": job_id,
            "status": data.get("status", "UNKNOWN"),
            "result": result,
            "error": data.get("error") or None,
        }

    def get_batch(self, batch_id: str) -> dict | None:
        batch_key = f"{BATCH_KEY_PREFIX}{batch_id}"
        job_ids = self._client.lrange(batch_key, 0, -1)
        if not job_ids:
            return None
        by_status = {"QUEUED": 0, "PROCESSING": 0, "DONE": 0, "FAILED": 0}
        jobs = []
        for jid in job_ids:
            job = self.get_job(jid)
            status = job["status"] if job else "UNKNOWN"
            by_status[status] = by_status.get(status, 0) + 1
            jobs.append({
                "job_id": jid,
                "status": status,
                "result": job["result"] if job else None,
                "error": job["error"] if job else None,
            })
        done = by_status.get("DONE", 0) + by_status.get("FAILED", 0)
        return {
            "batch_id": batch_id,
            "total": len(job_ids),
            "by_status": by_status,
            "complete": done == len(job_ids),
            "job_ids": job_ids,
            "jobs": jobs,
        }

    def stats(self) -> dict:
        depth = self._client.llen(self._queue_key)
        return {"queue_depth": depth, "backend": "redis"}

    async def _worker_loop(self, worker_id: int):
        while not self._stop_flag:
            try:
                item = await asyncio.to_thread(
                    self._client.brpop,
                    self._queue_key,
                    2,
                )
            except Exception as e:
                # A transient Redis connection issue must not permanently
                # kill this worker -- an unhandled exception in an asyncio
                # task just terminates it silently, quietly reducing worker
                # capacity with no visible error. Log, back off briefly,
                # and keep trying.
                logger.error(
                    f"Worker {worker_id} lost connection to Redis ({e}); "
                    f"retrying in 2s."
                )
                await asyncio.sleep(2)
                continue

            if item is None:
                continue  # timeout, loop back and check _stop_flag

            try:
                _, raw = item
                envelope = json.loads(raw)
                job_id = envelope["job_id"]
                payload = envelope["payload"]
            except Exception as e:
                logger.error(
                    f"Worker {worker_id} received an unparseable queue item "
                    f"({e}); dropping it."
                )
                continue

            job_key = f"{JOB_KEY_PREFIX}{job_id}"

            try:
                self._client.hset(job_key, "status", "PROCESSING")
            except Exception as e:
                logger.warning(
                    f"Worker {worker_id} could not mark job {job_id} "
                    f"PROCESSING ({e}); continuing anyway."
                )

            try:
                result = await asyncio.to_thread(self._submit_fn, payload)
                self._client.hset(
                    job_key,
                    mapping={
                        "status": "DONE",
                        "result": json.dumps(result),
                    },
                )
            except Exception as e:
                logger.exception(
                    f"Job {job_id} failed on worker {worker_id}"
                )
                try:
                    self._client.hset(
                        job_key,
                        mapping={
                            "status": "FAILED",
                            "error": str(e),
                        },
                    )
                except Exception as inner_e:
                    # Even reporting the failure back to Redis can itself fail
                    # (e.g. Redis went down mid-job) -- log it, but don't let
                    # a status-write failure crash the worker either.
                    logger.error(
                        f"Worker {worker_id} could not record failure "
                        f"for job {job_id}: {inner_e}"
                    )



def make_queue(submit_fn, num_workers: int = 4):
    """Factory: returns a RedisEvaluationQueue if QUEUE_BACKEND=redis and
    Redis is reachable at import/start time, otherwise the in-process
    EvaluationQueue. Mirrors make_cache()'s fallback behavior in
    redis_cache.py for the same reason -- dependency-free by default,
    one env var away from the shared-backend path."""
    import os
    from app.core.job_queue import EvaluationQueue

    backend = os.environ.get("QUEUE_BACKEND", "memory").lower()
    if backend == "redis":
        try:
            url = os.environ.get("REDIS_URL", "redis://localhost:6379/0")
            queue = RedisEvaluationQueue(submit_fn=submit_fn, num_workers=num_workers, redis_url=url)
            queue._client.ping()
            logger.info("Using Redis-backed evaluation queue.")
            return queue
        except Exception as e:
            logger.warning(f"QUEUE_BACKEND=redis but Redis unavailable ({e}); falling back to in-process queue.")
    return EvaluationQueue(submit_fn=submit_fn, num_workers=num_workers)

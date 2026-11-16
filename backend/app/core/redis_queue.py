"""Bounded Redis Streams queue with pending-entry recovery and lease renewal.

Delivery is at least once. Stable request IDs and the durable payment outbox make
redelivery safe. Completed results expire; pending jobs and batches do not.
"""

import asyncio
import json
import logging
import os
import uuid
import redis as redis_lib

logger = logging.getLogger("sentinel.redis_queue")
QUEUE_KEY = "sentinel:queue:v2"
JOB_TTL_SECONDS = 3600


class RedisEvaluationQueue:
    def __init__(
        self,
        submit_fn,
        num_workers=4,
        redis_url="redis://localhost:6379/0",
        queue_key=QUEUE_KEY,
        max_queue_size=500,
        lease_ms=60000,
    ):
        self._client = redis_lib.Redis.from_url(
            redis_url, decode_responses=True, socket_connect_timeout=3, socket_timeout=5
        )
        self._submit_fn, self._num_workers = submit_fn, num_workers
        self._queue_key, self._group = queue_key, "evaluators"
        self._prefix = queue_key + ":"
        self._workers = []
        self._lease_ms, self._capacity = lease_ms, max_queue_size
        self._consumer = uuid.uuid4().hex

    def _jobkey(self, jid):
        return self._prefix + "job:" + jid

    def _batchkey(self, bid):
        return self._prefix + "batch:" + bid

    async def start(self):
        await asyncio.to_thread(self._client.ping)
        try:
            self._client.xgroup_create(
                self._queue_key, self._group, id="0", mkstream=True
            )
        except redis_lib.ResponseError as exc:
            if "BUSYGROUP" not in str(exc):
                raise
        self._workers = [
            asyncio.create_task(self._worker_loop(i)) for i in range(self._num_workers)
        ]

    async def stop(self):
        for task in self._workers:
            task.cancel()
        await asyncio.gather(*self._workers, return_exceptions=True)
        self._workers = []

    async def enqueue_batch(self, payloads, batch_id=None):
        if not 1 <= len(payloads) <= 100:
            raise ValueError("batch size must be 1..100")
        jobs = []
        for payload in payloads:
            payload = dict(payload)
            payload.setdefault("request_id", "req_" + uuid.uuid4().hex)
            jobs.append(
                {
                    "job_id": "job_" + uuid.uuid4().hex,
                    "payload": payload,
                    "batch_id": batch_id,
                }
            )
        # Capacity check and all writes in one atomic script, including batch membership.
        script = """
        local jobs = cjson.decode(ARGV[1])
        if redis.call('XLEN', KEYS[1]) + #jobs > tonumber(ARGV[2]) then return 0 end
        for _,job in ipairs(jobs) do
          local jk = ARGV[3] .. 'job:' .. job.job_id
          redis.call('HSET', jk, 'status', 'QUEUED', 'result', '', 'error', '',
            'submitted_by', job.payload._submitted_by or '', 'attempts', '0')
          redis.call('XADD', KEYS[1], '*', 'envelope', cjson.encode(job))
          if ARGV[4] ~= '' then redis.call('RPUSH', ARGV[3] .. 'batch:' .. ARGV[4], job.job_id) end
        end
        return 1
        """
        ok = await asyncio.to_thread(
            self._client.eval,
            script,
            1,
            self._queue_key,
            json.dumps(jobs),
            self._capacity,
            self._prefix,
            batch_id or "",
        )
        if not ok:
            raise ValueError("Queue capacity exceeded; retry later")
        return [j["job_id"] for j in jobs]

    async def enqueue(self, payload, batch_id=None):
        return (await self.enqueue_batch([payload], batch_id))[0]

    def get_job(self, job_id):
        data = self._client.hgetall(self._jobkey(job_id))
        if not data:
            return None
        return {
            "job_id": job_id,
            "status": data["status"],
            "result": json.loads(data["result"]) if data.get("result") else None,
            "error": data.get("error") or None,
            "submitted_by": data.get("submitted_by", ""),
        }

    def get_batch(self, batch_id):
        ids = self._client.lrange(self._batchkey(batch_id), 0, -1)
        if not ids:
            return None
        jobs = [
            self.get_job(jid)
            or {
                "job_id": jid,
                "status": "EXPIRED",
                "result": None,
                "error": "Result expired",
            }
            for jid in ids
        ]
        counts = {
            key: 0 for key in ("QUEUED", "PROCESSING", "DONE", "FAILED", "EXPIRED")
        }
        for job in jobs:
            counts[job["status"]] += 1
        return {
            "batch_id": batch_id,
            "total": len(ids),
            "by_status": counts,
            "complete": all(j["status"] in ("DONE", "FAILED", "EXPIRED") for j in jobs),
            "job_ids": ids,
            "jobs": jobs,
        }

    def stats(self):
        self._client.ping()
        return {
            "backend": "redis",
            "queue_depth": self._client.xlen(self._queue_key),
            "live_workers": sum(not t.done() for t in self._workers),
        }

    async def _heartbeat(self, consumer, message_id):
        while True:
            await asyncio.sleep(max(0.05, self._lease_ms / 3000))
            await asyncio.to_thread(
                self._client.xclaim,
                self._queue_key,
                self._group,
                consumer,
                0,
                [message_id],
                justid=True,
            )

    async def _worker_loop(self, worker_id):
        consumer = f"{self._consumer}:{worker_id}"
        while True:
            try:
                claimed = await asyncio.to_thread(
                    self._client.xautoclaim,
                    self._queue_key,
                    self._group,
                    consumer,
                    self._lease_ms,
                    "0-0",
                    count=1,
                )
                messages = claimed[1]
                if not messages:
                    rows = await asyncio.to_thread(
                        self._client.xreadgroup,
                        self._group,
                        consumer,
                        {self._queue_key: ">"},
                        count=1,
                        block=500,
                    )
                    messages = rows[0][1] if rows else []
                if not messages:
                    continue
                mid, data = messages[0]
                envelope = json.loads(data["envelope"])
                jid, payload = envelope["job_id"], envelope["payload"]
                key = self._jobkey(jid)
                heartbeat = asyncio.create_task(self._heartbeat(consumer, mid))
                try:
                    self._client.hset(key, "status", "PROCESSING")
                    attempts = self._client.hincrby(key, "attempts", 1)
                    if attempts > 5:
                        raise ValueError("Retry budget exhausted")
                    result = await asyncio.to_thread(self._submit_fn, payload)
                    result_json, error, status = json.dumps(result), "", "DONE"
                except asyncio.CancelledError:
                    raise
                except Exception:
                    logger.exception("Evaluation job failed")
                    result_json, error, status = (
                        "",
                        "Evaluation failed; inspect server logs",
                        "FAILED",
                    )
                finally:
                    heartbeat.cancel()
                    await asyncio.gather(heartbeat, return_exceptions=True)
                # Completion and acknowledgement are atomic. If Redis fails before this
                # transaction, the entry remains pending and is reclaimed after the lease.
                pipe = self._client.pipeline(transaction=True)
                pipe.hset(
                    key,
                    mapping={"status": status, "result": result_json, "error": error},
                )
                pipe.expire(key, JOB_TTL_SECONDS)
                pipe.xack(self._queue_key, self._group, mid)
                pipe.xdel(self._queue_key, mid)
                await asyncio.to_thread(pipe.execute)
                batch_id = envelope.get("batch_id")
                if batch_id:
                    batch = self.get_batch(batch_id)
                    if batch and batch["complete"]:
                        self._client.expire(self._batchkey(batch_id), JOB_TTL_SECONDS)
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception(
                    "Queue dependency failure; pending work remains recoverable"
                )
                await asyncio.sleep(1)


def make_queue(submit_fn, num_workers=4):
    if os.environ.get("QUEUE_BACKEND", "memory") == "redis":
        # Never silently fall back: replicas must agree on queue ownership.
        return RedisEvaluationQueue(
            submit_fn,
            num_workers,
            os.environ.get("REDIS_URL", "redis://localhost:6379/0"),
        )
    from app.core.job_queue import EvaluationQueue

    return EvaluationQueue(submit_fn, num_workers)

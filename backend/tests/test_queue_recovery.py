import asyncio
import os
import uuid
import pytest
import redis
from app.core.redis_queue import RedisEvaluationQueue

URL = os.environ.get("REDIS_URL", "redis://localhost:6379/0")
try:
    redis.Redis.from_url(URL, socket_connect_timeout=1, socket_timeout=1).ping()
    AVAILABLE = True
except Exception:
    AVAILABLE = False
pytestmark = pytest.mark.skipif(not AVAILABLE, reason="Isolated Redis unavailable")


def test_reclaim_orphaned_delivery():
    async def run():
        calls = []
        q = RedisEvaluationQueue(
            lambda p: calls.append(p["request_id"]) or {"value": p["value"]},
            redis_url=URL,
            queue_key="sentinel:test:" + uuid.uuid4().hex,
            lease_ms=100,
        )
        # Simulate a dead consumer claiming work without acknowledging it.
        q._client.xgroup_create(q._queue_key, q._group, id="0", mkstream=True)
        jid = await q.enqueue({"value": 42})
        q._client.xreadgroup(q._group, "dead-consumer", {q._queue_key: ">"}, count=1)
        await asyncio.sleep(0.15)
        await q.start()
        try:
            for _ in range(100):
                job = q.get_job(jid)
                if job["status"] == "DONE":
                    break
                await asyncio.sleep(0.02)
            assert job["result"] == {"value": 42}
            assert len(calls) == 1
            assert q._client.xpending(q._queue_key, q._group)["pending"] == 0
        finally:
            await q.stop()

    asyncio.run(run())


def test_capacity_failure_is_atomic():
    async def run():
        q = RedisEvaluationQueue(
            lambda x: x,
            redis_url=URL,
            queue_key="sentinel:test:" + uuid.uuid4().hex,
            max_queue_size=1,
        )
        q._client.xgroup_create(q._queue_key, q._group, id="0", mkstream=True)
        await q.enqueue({"value": 1})
        with pytest.raises(ValueError):
            await q.enqueue_batch([{"value": 2}, {"value": 3}], batch_id="batch")
        assert q.get_batch("batch") is None
        assert q._client.xlen(q._queue_key) == 1

    asyncio.run(run())

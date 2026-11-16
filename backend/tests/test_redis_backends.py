"""
Tests for the Redis-backed cache and queue.
Requires a running Redis instance (see docker-compose.yml, or `service
redis-server start` locally). Skipped automatically if Redis isn't
reachable, so the main test suite doesn't hard-depend on Redis being up.
"""
import sys
import os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import pytest
import asyncio

try:
    import redis as redis_lib
    _client = redis_lib.Redis.from_url(os.environ.get("REDIS_URL", "redis://localhost:6379/0"), socket_connect_timeout=1, socket_timeout=1)
    _client.ping()
    REDIS_AVAILABLE = True
except Exception:
    REDIS_AVAILABLE = False

pytestmark = pytest.mark.skipif(not REDIS_AVAILABLE, reason="Redis not reachable")


class TestRedisCache:
    def test_set_get_roundtrip(self):
        from app.core.redis_cache import RedisCache
        cache = RedisCache(ttl_seconds=5, key_prefix="test:cache:")
        cache.set("k1", {"a": 1, "b": [1, 2, 3]})
        assert cache.get("k1") == {"a": 1, "b": [1, 2, 3]}
        cache.invalidate("k1")

    def test_missing_key_returns_none(self):
        from app.core.redis_cache import RedisCache
        cache = RedisCache(ttl_seconds=5, key_prefix="test:cache:")
        assert cache.get("does_not_exist") is None

    def test_invalidate_prefix(self):
        from app.core.redis_cache import RedisCache
        cache = RedisCache(ttl_seconds=5, key_prefix="test:cache:")
        cache.set("agent_1:foo", {"v": 1})
        cache.set("agent_1:bar", {"v": 2})
        cache.set("agent_2:baz", {"v": 3})
        cache.invalidate_prefix("agent_1:")
        assert cache.get("agent_1:foo") is None
        assert cache.get("agent_1:bar") is None
        assert cache.get("agent_2:baz") == {"v": 3}
        cache.invalidate("agent_2:baz")

    def test_ttl_expiry(self):
        import time
        from app.core.redis_cache import RedisCache
        cache = RedisCache(ttl_seconds=1, key_prefix="test:cache:")
        cache.set("expiring", {"v": 1})
        assert cache.get("expiring") == {"v": 1}
        time.sleep(1.5)
        assert cache.get("expiring") is None


class TestRedisQueue:
    def test_enqueue_and_process(self):
        from app.core.redis_queue import RedisEvaluationQueue

        def fake_submit(payload):
            return {"echo": payload["value"] * 2}

        async def run():
            queue = RedisEvaluationQueue(
                submit_fn=fake_submit,
                num_workers=2,
                queue_key="sentinel:test:queue:" + __import__("uuid").uuid4().hex,
                redis_url=os.environ.get("REDIS_URL", "redis://localhost:6379/0"),
            )
            await queue.start()
            job_id = await queue.enqueue({"value": 21})
            job = None
            for _ in range(50):
                job = queue.get_job(job_id)
                if job["status"] in ("DONE", "FAILED"):
                    break
                await asyncio.sleep(0.05)
            await queue.stop()
            return job

        job = asyncio.run(run())
        assert job["status"] == "DONE"
        assert job["result"] == {"echo": 42}

    def test_failed_job_captures_error(self):
        from app.core.redis_queue import RedisEvaluationQueue

        def failing_submit(payload):
            raise ValueError("intentional test failure")

        async def run():
            queue = RedisEvaluationQueue(
                submit_fn=failing_submit,
                num_workers=1,
                queue_key="sentinel:test:queue:" + __import__("uuid").uuid4().hex,
                redis_url=os.environ.get("REDIS_URL", "redis://localhost:6379/0"),
            )
            await queue.start()
            job_id = await queue.enqueue({"value": 1})
            job = None
            for _ in range(50):
                job = queue.get_job(job_id)
                if job["status"] in ("DONE", "FAILED"):
                    break
                await asyncio.sleep(0.05)
            await queue.stop()
            return job

        job = asyncio.run(run())
        assert job["status"] == "FAILED"
        assert job["error"] == "Evaluation failed; inspect server logs"

    def test_unknown_job_id_returns_none(self):
        from app.core.redis_queue import RedisEvaluationQueue
        queue = RedisEvaluationQueue(submit_fn=lambda p: p, num_workers=1, redis_url=os.environ.get("REDIS_URL", "redis://localhost:6379/0"))
        assert queue.get_job("job_does_not_exist") is None


class TestFactoryFallback:
    def test_make_cache_falls_back_when_backend_is_memory(self):
        from app.core.redis_cache import make_cache
        from app.core.cache import TTLCache
        os.environ["CACHE_BACKEND"] = "memory"
        cache = make_cache(ttl_seconds=5, key_prefix="test:")
        assert isinstance(cache, TTLCache)

    def test_make_queue_falls_back_when_backend_is_memory(self):
        from app.core.redis_queue import make_queue
        from app.core.job_queue import EvaluationQueue
        os.environ["QUEUE_BACKEND"] = "memory"
        queue = make_queue(submit_fn=lambda p: p)
        assert isinstance(queue, EvaluationQueue)


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))

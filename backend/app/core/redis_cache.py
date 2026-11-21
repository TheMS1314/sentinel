"""
Redis Cache Backend
=====================
A Redis-backed implementation of the same interface as TTLCache
(cache.py) -- get/set/invalidate/invalidate_prefix/stats. This is the
"swap to a shared backend" step that cache.py's own docstring predicted
would be needed once there's a real multi-instance deployment: several
backend replicas behind a load balancer need to see the SAME cached
policy/case-index state, which an in-process dict can't provide.

Values are JSON-serialized. `invalidate_prefix` uses Redis SCAN (not
KEYS, which blocks the whole server on a large keyspace) to find
matching keys safely even under concurrent load.
"""

from __future__ import annotations
import json
import os
import logging

logger = logging.getLogger("sentinel.redis_cache")

try:
    import redis as redis_lib
except ImportError:
    redis_lib = None


class RedisCache:
    def __init__(self, ttl_seconds: float = 30.0, key_prefix: str = "sentinel:cache:"):
        self.ttl = ttl_seconds
        self.key_prefix = key_prefix
        url = os.environ.get("REDIS_URL", "redis://localhost:6379/0")
        if redis_lib is None:
            raise RuntimeError("redis package not installed -- pip install redis")
        self._client = redis_lib.Redis.from_url(
            url,
            decode_responses=True,
            socket_connect_timeout=2,
            socket_timeout=2,
            max_connections=16,
        )

    def _k(self, key: str) -> str:
        return f"{self.key_prefix}{key}"

    def get(self, key: str):
        try:
            raw = self._client.get(self._k(key))
        except Exception as e:
            logger.warning(
                "Redis GET unavailable (%s); treating as a cache miss.",
                type(e).__name__,
            )
            return None
        if raw is None:
            return None
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            logger.warning("Corrupt cache value; dropping it.")
            try:
                self._client.delete(self._k(key))
            except Exception:
                pass  # best-effort cleanup; the corrupt value will simply expire via TTL
            return None

    def set(self, key: str, value):
        try:
            self._client.set(self._k(key), json.dumps(value), ex=int(self.ttl))
        except Exception as e:
            # Caching is an optimization, never a correctness requirement --
            # a failed cache write must not fail the request that triggered
            # it. The next read will just be a cache miss and recompute.
            logger.warning(
                "Redis SET unavailable (%s); continuing without cache.",
                type(e).__name__,
            )

    def invalidate(self, key: str):
        try:
            self._client.delete(self._k(key))
        except Exception as e:
            logger.warning(
                "Redis DELETE unavailable (%s); value expires by TTL.", type(e).__name__
            )

    def invalidate_prefix(self, prefix: str):
        try:
            pattern = f"{self.key_prefix}{prefix}*"
            cursor = 0
            for _ in range(100):
                cursor, keys = self._client.scan(
                    cursor=cursor, match=pattern, count=100
                )
                if keys:
                    self._client.delete(*keys)
                if cursor == 0:
                    break
            else:
                logger.warning(
                    "Redis cache invalidation scan bound reached; remaining entries expire by TTL"
                )
        except Exception as e:
            logger.warning(
                "Redis cache invalidation unavailable (%s); remaining entries expire by TTL.",
                type(e).__name__,
            )

    def stats(self) -> dict:
        pattern = f"{self.key_prefix}*"
        cursor = 0
        count = 0
        for _ in range(100):
            cursor, keys = self._client.scan(cursor=cursor, match=pattern, count=100)
            count += len(keys)
            if cursor == 0:
                break
        return {
            "total_entries": count,
            "live_entries": count,
            "backend": "redis",
            "truncated": cursor != 0,
        }


def make_cache(ttl_seconds: float, key_prefix: str):
    """Factory: returns a RedisCache if CACHE_BACKEND=redis and Redis is
    reachable, otherwise falls back to the in-memory TTLCache. This keeps
    local/dev usage dependency-free by default while making the
    multi-instance path a one-line env var away, exactly as designed."""
    from app.core.cache import TTLCache

    backend = os.environ.get("CACHE_BACKEND", "memory").lower()
    if backend == "redis":
        try:
            cache = RedisCache(ttl_seconds=ttl_seconds, key_prefix=key_prefix)
            cache._client.ping()
            logger.info(f"Using Redis cache backend for {key_prefix}")
            return cache
        except Exception as e:
            logger.warning(
                "Redis cache unavailable (%s); using bounded memory cache.",
                type(e).__name__,
            )
    return TTLCache(ttl_seconds=ttl_seconds)

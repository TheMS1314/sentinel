"""
Rate Limiting
==============
A simple per-API-key sliding-window rate limiter. Applied as a FastAPI
dependency alongside require_api_key -- every authenticated caller gets
its own budget, so one noisy/misbehaving caller (or a bug in a
purchasing agent that starts hammering the API) can't starve every
other agent's access to the trust layer that's supposed to be
protecting them.

Design: in-memory sliding window (deque of timestamps per key),
sufficient for a single-process instance. For the multi-replica
deployment, this would need to move to Redis (INCR + EXPIRE, or a
Lua script for atomicity) so the limit is enforced across all
replicas rather than per-replica -- documented here as the same kind
of "shared backend" step already taken for cache.py and job_queue.py,
not implemented, since it's not needed to demonstrate the core
rate-limiting behavior.
"""

from __future__ import annotations
import time
import os
from collections import defaultdict, deque
from threading import Lock
from fastapi import Request, HTTPException
import asyncio
import hashlib
import uuid

RATE_LIMIT_REQUESTS = int(os.environ.get("SENTINEL_RATE_LIMIT_REQUESTS", "60"))
RATE_LIMIT_WINDOW_SECONDS = int(
    os.environ.get("SENTINEL_RATE_LIMIT_WINDOW_SECONDS", "60")
)


class SlidingWindowRateLimiter:
    def __init__(self, max_requests: int, window_seconds: int):
        self.max_requests = max_requests
        self.window_seconds = window_seconds
        self._hits: dict = defaultdict(deque)
        self._lock = Lock()

    def check(self, key: str):
        """Returns (allowed, retry_after_seconds)."""
        now = time.time()
        with self._lock:
            if len(self._hits) > 10000:
                self._hits = defaultdict(
                    deque,
                    {
                        k: v
                        for k, v in self._hits.items()
                        if v and v[-1] >= now - self.window_seconds
                    },
                )
            window = self._hits[key]
            cutoff = now - self.window_seconds
            while window and window[0] < cutoff:
                window.popleft()

            if len(window) >= self.max_requests:
                retry_after = int(window[0] + self.window_seconds - now) + 1
                return False, retry_after

            window.append(now)
            return True, 0

    def reset(self):
        """Test-only helper."""
        with self._lock:
            self._hits.clear()


rate_limiter = SlidingWindowRateLimiter(RATE_LIMIT_REQUESTS, RATE_LIMIT_WINDOW_SECONDS)


async def enforce_rate_limit(request: Request):
    principal = getattr(request.state, "principal", None)
    key = (
        principal.subject
        if principal
        else "login:" + (request.client.host if request.client else "unknown")
    )
    if os.environ.get("RATE_LIMIT_BACKEND") == "redis":

        def check_shared():
            import redis

            client = redis.Redis.from_url(
                os.environ.get("REDIS_URL", "redis://localhost:6379/0"),
                socket_connect_timeout=2,
                socket_timeout=2,
            )
            script = """
            local t = redis.call('TIME')
            local now = t[1] * 1000 + math.floor(t[2] / 1000)
            local window = tonumber(ARGV[1]) * 1000
            redis.call('ZREMRANGEBYSCORE', KEYS[1], '-inf', now - window)
            if redis.call('ZCARD', KEYS[1]) >= tonumber(ARGV[2]) then
              local first = redis.call('ZRANGE', KEYS[1], 0, 0, 'WITHSCORES')
              return math.max(1, math.ceil((tonumber(first[2]) + window - now) / 1000))
            end
            redis.call('ZADD', KEYS[1], now, ARGV[3])
            redis.call('EXPIRE', KEYS[1], ARGV[1])
            return 0
            """
            retry = client.eval(
                script,
                1,
                "sentinel:rate:" + hashlib.sha256(key.encode()).hexdigest(),
                RATE_LIMIT_WINDOW_SECONDS,
                RATE_LIMIT_REQUESTS,
                uuid.uuid4().hex,
            )
            return retry == 0, retry

        try:
            allowed, retry_after = await asyncio.to_thread(check_shared)
        except Exception:
            raise HTTPException(503, "Rate-limit backend unavailable") from None
    else:
        allowed, retry_after = rate_limiter.check(key)
    if not allowed:
        raise HTTPException(
            429, "Rate limit exceeded", headers={"Retry-After": str(retry_after)}
        )

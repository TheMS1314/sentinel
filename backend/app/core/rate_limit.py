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
from fastapi import Header, HTTPException

RATE_LIMIT_REQUESTS = int(os.environ.get("SENTINEL_RATE_LIMIT_REQUESTS", "60"))
RATE_LIMIT_WINDOW_SECONDS = int(os.environ.get("SENTINEL_RATE_LIMIT_WINDOW_SECONDS", "60"))


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


async def enforce_rate_limit(x_api_key: str = Header(default="")):
    key = x_api_key or "anonymous"
    allowed, retry_after = rate_limiter.check(key)
    if not allowed:
        raise HTTPException(
            status_code=429,
            detail=f"Rate limit exceeded: {RATE_LIMIT_REQUESTS} requests per {RATE_LIMIT_WINDOW_SECONDS}s.",
            headers={"Retry-After": str(retry_after)},
        )

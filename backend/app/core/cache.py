"""
TTL Cache
==========
A small in-memory cache with time-based expiry and explicit invalidation.

Design choice -- in-memory, not Redis:
  At this scale (a single backend instance, a demo/eval workload), an
  external cache service adds an operational dependency (another thing
  to start, another thing that can fail) for no real benefit -- there's
  no multi-instance cache-sharing problem to solve yet. The cache API
  here is intentionally small enough that swapping the backing store
  for Redis later (once there's an actual multi-instance deployment) is
  a contained change: only this file would need to change, nothing that
  calls it.

Used for:
  - Policy lookups (GET /policies/{agent_id}) -- policies change rarely
    but are read on every single transaction evaluation.
  - The RAG case retriever's per-agent index -- rebuilding it from the
    full audit history on every escalation is wasteful when most
    escalations for the same agent happen close together in time.
"""

from __future__ import annotations
import time
from threading import Lock


class TTLCache:
    def __init__(self, ttl_seconds: float = 30.0):
        self.ttl = ttl_seconds
        self._store: dict[str, tuple[float, object]] = {}
        self._lock = Lock()

    def get(self, key: str):
        with self._lock:
            entry = self._store.get(key)
            if entry is None:
                return None
            expires_at, value = entry
            if time.time() > expires_at:
                del self._store[key]
                return None
            return value

    def set(self, key: str, value):
        with self._lock:
            self._store[key] = (time.time() + self.ttl, value)

    def invalidate(self, key: str):
        with self._lock:
            self._store.pop(key, None)

    def invalidate_prefix(self, prefix: str):
        """Invalidate all keys starting with `prefix` -- used when a
        write affects a whole family of cached entries (e.g. any new
        decision for agent X should invalidate agent X's case index,
        regardless of the exact cache key used)."""
        with self._lock:
            to_remove = [k for k in self._store if k.startswith(prefix)]
            for k in to_remove:
                del self._store[k]

    def stats(self) -> dict:
        with self._lock:
            now = time.time()
            live = sum(1 for exp, _ in self._store.values() if exp > now)
            return {"total_entries": len(self._store), "live_entries": live}


# Module-level singletons.
#
# policy_cache: JSON-safe values (plain dicts), so it CAN move to Redis
# via CACHE_BACKEND=redis for a shared cache across multiple backend
# replicas -- see redis_cache.py.
#
# case_index_cache: intentionally ALWAYS in-memory, never Redis, even
# when CACHE_BACKEND=redis is set. It stores a fitted scikit-learn
# TfidfVectorizer and a sparse matrix -- neither is JSON-serializable,
# and pickling arbitrary sklearn objects into a shared cache is its own
# can of worms (version skew between replicas, deserialization trust).
# Recomputing this index per-process is cheap enough (see case_retriever.py)
# that each replica keeping its own is the right tradeoff, not a gap.
def _init_policy_cache():
    from app.core.redis_cache import make_cache
    return make_cache(ttl_seconds=60.0, key_prefix="sentinel:policy:")


policy_cache = _init_policy_cache()
case_index_cache = TTLCache(ttl_seconds=15.0)

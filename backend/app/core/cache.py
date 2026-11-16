"""Optional TTL cache utility; authorization always reads durable policy state."""

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

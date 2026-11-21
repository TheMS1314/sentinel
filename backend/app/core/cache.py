"""Optional TTL cache utility; authorization always reads durable policy state."""

from __future__ import annotations
import time
import math
from collections import OrderedDict
from threading import Lock


class TTLCache:
    def __init__(self, ttl_seconds: float = 30.0, max_entries: int = 1024):
        if (
            not math.isfinite(ttl_seconds)
            or ttl_seconds <= 0
            or type(max_entries) is not int
            or max_entries < 1
        ):
            raise ValueError(
                "Cache needs a finite positive TTL and positive entry bound"
            )
        self.ttl = ttl_seconds
        self.max_entries = max_entries
        self._store = OrderedDict()
        self._lock = Lock()

    def get(self, key: str):
        with self._lock:
            entry = self._store.get(key)
            if entry is None:
                return None
            expires_at, value = entry
            if time.monotonic() >= expires_at:
                del self._store[key]
                return None
            self._store.move_to_end(key)
            return value

    def set(self, key: str, value):
        with self._lock:
            now = time.monotonic()
            self._prune(now)
            self._store[key] = (now + self.ttl, value)
            self._store.move_to_end(key)
            while len(self._store) > self.max_entries:
                self._store.popitem(last=False)

    def _prune(self, now):
        for key in [k for k, (expiry, _) in self._store.items() if expiry <= now]:
            del self._store[key]

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
            self._prune(time.monotonic())
            return {"total_entries": len(self._store), "live_entries": len(self._store)}

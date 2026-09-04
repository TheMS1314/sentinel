"""
Tests for the sliding-window rate limiter.
Run with: pytest backend/tests/test_rate_limit.py -v
"""
import sys
import os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import pytest
import time
from app.core.rate_limit import SlidingWindowRateLimiter


class TestSlidingWindowRateLimiter:
    def test_allows_up_to_the_limit(self):
        limiter = SlidingWindowRateLimiter(max_requests=5, window_seconds=60)
        for _ in range(5):
            allowed, _ = limiter.check("key1")
            assert allowed is True

    def test_blocks_beyond_the_limit(self):
        limiter = SlidingWindowRateLimiter(max_requests=3, window_seconds=60)
        for _ in range(3):
            limiter.check("key1")
        allowed, retry_after = limiter.check("key1")
        assert allowed is False
        assert retry_after > 0

    def test_different_keys_have_independent_budgets(self):
        limiter = SlidingWindowRateLimiter(max_requests=2, window_seconds=60)
        limiter.check("agent_a")
        limiter.check("agent_a")
        allowed_a, _ = limiter.check("agent_a")
        allowed_b, _ = limiter.check("agent_b")
        assert allowed_a is False
        assert allowed_b is True

    def test_window_expiry_allows_requests_again(self):
        limiter = SlidingWindowRateLimiter(max_requests=1, window_seconds=1)
        allowed1, _ = limiter.check("key1")
        assert allowed1 is True
        allowed2, _ = limiter.check("key1")
        assert allowed2 is False
        time.sleep(1.1)
        allowed3, _ = limiter.check("key1")
        assert allowed3 is True

    def test_reset_clears_all_state(self):
        limiter = SlidingWindowRateLimiter(max_requests=1, window_seconds=60)
        limiter.check("key1")
        allowed, _ = limiter.check("key1")
        assert allowed is False
        limiter.reset()
        allowed_after_reset, _ = limiter.check("key1")
        assert allowed_after_reset is True


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))

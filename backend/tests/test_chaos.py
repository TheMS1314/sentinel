"""
Chaos / Failure-Mode Tests
=============================
Verifies HOW each external dependency fails, not just that the happy
path works. The design intent, tested explicitly here:

  FAIL-SOFT  (degrade gracefully, never crash the request):
    - LLM reasoning call        -> defaults to a safe HOLD, not a 500
    - Cache (Redis)              -> treated as a miss/no-op, recomputes
    - Razorpay payment gateway   -> records a failed/mock status,
                                     doesn't crash the decision itself
                                     (the DECISION already happened;
                                     only the payment attempt failed)

  FAIL-CLOSED (must propagate loudly, never silently continue):
    - The audit database         -> if Sentinel can't durably log a
                                     transaction and its decision, it
                                     must not pretend the decision
                                     happened. Silently proceeding
                                     without an audit record would
                                     violate the entire premise of an
                                     auditable trust layer. This is a
                                     DELIBERATE asymmetry, not an
                                     oversight -- optimizations may
                                     degrade, the audit trail may not.
"""
import sys
import os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
os.environ["OPENROUTER_API_KEY"] = ""

import pytest
from unittest.mock import MagicMock
import uuid

from app.models.db import init_db, SessionLocal
from app.models.policy import AgentPolicy, TransactionRequest, TimeWindow, VelocityLimit, VERDICT_HOLD
from app.core.orchestrator import SentinelOrchestrator
from app.core.llm_client import LLMClient
from app.core.llm_reasoner import LLMReasoner


@pytest.fixture(scope="module", autouse=True)
def setup_db():
    init_db()
    yield


@pytest.fixture
def db():
    session = SessionLocal()
    yield session
    session.close()


def fresh_agent_id() -> str:
    return f"agent_chaos_{uuid.uuid4().hex[:8]}"


def make_policy(agent_id: str, **overrides) -> AgentPolicy:
    defaults = dict(
        policy_id=f"pol_{agent_id}", agent_id=agent_id, owner_human_id="human_chaos",
        max_amount_per_txn=2000.0, max_daily_total=5000.0,
        allowed_categories=["grocery"], time_window=TimeWindow(6, 22), velocity=VelocityLimit(3, 1440),
    )
    defaults.update(overrides)
    return AgentPolicy(**defaults)


def make_request(agent_id: str, **overrides) -> TransactionRequest:
    defaults = dict(
        request_id=f"req_{uuid.uuid4().hex[:10]}", agent_id=agent_id,
        merchant_name="FreshMart", merchant_category="grocery", amount=500.0,
        requested_at_hour=14, requested_at_iso="2026-08-24T14:00:00",
        stated_intent="restock kitchen supplies",
        raw_agent_goal="buy weekly groceries under budget",
    )
    defaults.update(overrides)
    return TransactionRequest(**defaults)


class TestLLMFailsSoft:
    def test_llm_client_network_error_returns_error_dict_not_exception(self):
        client = LLMClient()
        client.mock_mode = False
        client._client = MagicMock()
        client.provider = "anthropic"
        client._client.messages.create.side_effect = ConnectionError("simulated network failure")

        result = client.complete_json("system", "user", mock_response={"should": "not_appear"})
        assert "error" in result
        assert result["error"] == "llm_call_failed"

    def test_reasoner_defaults_to_hold_when_llm_client_fails(self):
        agent_id = fresh_agent_id()
        policy = make_policy(agent_id)
        request = make_request(agent_id)

        broken_client = LLMClient()
        broken_client.mock_mode = False
        broken_client._client = MagicMock()
        broken_client.provider = "anthropic"
        broken_client._client.messages.create.side_effect = TimeoutError("simulated timeout")

        reasoner = LLMReasoner(llm_client=broken_client)
        decision = reasoner.evaluate(policy, request)

        assert decision.verdict == VERDICT_HOLD
        assert decision.decided_by == "llm_reasoner"

    def test_full_orchestrator_survives_llm_outage(self, db):
        agent_id = fresh_agent_id()
        policy = make_policy(agent_id)
        request = make_request(agent_id)

        orchestrator = SentinelOrchestrator(db)
        orchestrator.reasoner.llm = MagicMock()
        orchestrator.reasoner.llm.mock_mode = False
        orchestrator.reasoner.llm.complete_json.return_value = {"error": "llm_call_failed", "detail": "simulated"}

        decision = orchestrator.evaluate_transaction(policy, request)
        assert decision.verdict == VERDICT_HOLD


class TestCacheFailsSoft:
    def test_redis_cache_get_failure_returns_none_not_exception(self):
        pytest.importorskip("redis")
        from app.core.redis_cache import RedisCache

        cache = RedisCache(ttl_seconds=10, key_prefix="test:chaos:")
        cache._client = MagicMock()
        cache._client.get.side_effect = ConnectionError("simulated redis outage")

        result = cache.get("some_key")
        assert result is None

    def test_redis_cache_set_failure_does_not_raise(self):
        pytest.importorskip("redis")
        from app.core.redis_cache import RedisCache

        cache = RedisCache(ttl_seconds=10, key_prefix="test:chaos:")
        cache._client = MagicMock()
        cache._client.set.side_effect = ConnectionError("simulated redis outage")

        cache.set("some_key", {"data": 1})

    def test_redis_cache_invalidate_failure_does_not_raise(self):
        pytest.importorskip("redis")
        from app.core.redis_cache import RedisCache

        cache = RedisCache(ttl_seconds=10, key_prefix="test:chaos:")
        cache._client = MagicMock()
        cache._client.delete.side_effect = ConnectionError("simulated redis outage")

        cache.invalidate("some_key")
        cache.invalidate_prefix("some_prefix")


class TestRazorpayFailsSoft:
    def test_gateway_exception_is_captured_not_raised(self):
        from app.core.razorpay_gateway import RazorpayGateway

        gateway = RazorpayGateway()
        gateway.mock_mode = False
        gateway.client = MagicMock()
        gateway.client.order.create.side_effect = ConnectionError("simulated Razorpay outage")

        result = gateway.create_order(amount_inr=500.0, request_id="req_chaos_1", notes={})
        assert result["status"] == "failed"
        assert "error" in result

    def test_orchestrator_completes_even_when_payment_gateway_fails(self, db):
        agent_id = fresh_agent_id()
        policy = make_policy(agent_id)
        request = make_request(agent_id, stated_intent="buy groceries")

        orchestrator = SentinelOrchestrator(db)
        orchestrator.gateway.mock_mode = False
        orchestrator.gateway.client = MagicMock()
        orchestrator.gateway.client.order.create.side_effect = ConnectionError("simulated outage")

        decision = orchestrator.evaluate_transaction(policy, request)
        assert decision.verdict == "APPROVE"


class TestDatabaseFailsClosed:
    """The audit database is the one dependency that must NOT fail
    soft. If we can't durably log a transaction, we must not pretend
    the evaluation happened -- silently proceeding without an audit
    record would violate the entire premise of this system."""

    def test_db_session_failure_propagates_not_swallowed(self, db):
        agent_id = fresh_agent_id()
        policy = make_policy(agent_id)
        request = make_request(agent_id, stated_intent="buy groceries")

        orchestrator = SentinelOrchestrator(db)
        orchestrator.db.add = MagicMock(side_effect=ConnectionError("simulated DB outage"))

        with pytest.raises(ConnectionError):
            orchestrator.evaluate_transaction(policy, request)


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))

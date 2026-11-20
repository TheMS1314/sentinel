"""Unusable provider payloads cannot become payment evidence; caches stay bounded."""

import hashlib
import json
from unittest.mock import MagicMock, patch
import pytest
from app.core.cache import TTLCache
from app.core.redis_cache import RedisCache
from app.core.razorpay_gateway import RazorpayGateway


def order(**changes):
    return dict(
        dict(
            id="order_Valid123",
            amount=1234,
            currency="INR",
            status="created",
            receipt=hashlib.sha256(b"request").hexdigest()[:40],
            notes=dict(sentinel_request_id="request", sentinel_merchant_id="merchant"),
        ),
        **changes,
    )


def gateway():
    result = RazorpayGateway()
    result.mock_mode = False
    result.client = MagicMock()
    return result


@pytest.mark.parametrize(
    "payload",
    [
        [],
        {},
        order(id="arbitrary"),
        order(amount=True),
        order(amount=1234.0),
        order(amount=1235),
        order(currency="USD"),
        order(status="unexpected"),
        order(notes=[]),
        order(notes={}),
        order(receipt="different"),
    ],
)
def test_unusable_create_response_stays_unknown_and_never_retries(payload):
    client = gateway()
    client.client.order.create.return_value = payload
    response = client.create_order(
        12.34, "request", dict(sentinel_merchant_id="merchant")
    )
    assert response["status"] == "unknown"
    assert response["order_id"] is None
    assert client.client.order.create.call_count == 1
    client.client.session.close.assert_called_once()


def test_creation_is_correlated_and_reconciliation_checks_actual_response_id():
    client = gateway()
    client.client.order.create.return_value = order()
    assert (
        client.create_order(12.34, "request", dict(sentinel_merchant_id="merchant"))[
            "status"
        ]
        == "created"
    )
    client.client.order.fetch.return_value = order(id="order_Different123")
    with pytest.raises(ValueError):
        client.reconcile("order_Valid123", "request", 1234, "merchant")
    client.client.order.fetch.return_value = order(status="paid")
    assert (
        client.reconcile("order_Valid123", "request", 1234, "merchant")["status"]
        == "captured"
    )
    client.client.order.fetch.return_value = order(status="invented")
    with pytest.raises(ValueError):
        client.reconcile("order_Valid123", "request", 1234, "merchant")


def test_gateway_errors_do_not_log_provider_exception_content(caplog):
    client = gateway()
    client.client.order.create.side_effect = RuntimeError(
        "SENSITIVE_PROVIDER_BODY_AND_KEY"
    )
    assert client.create_order(12.34, "request", {})["status"] == "unknown"
    assert "SENSITIVE_PROVIDER_BODY_AND_KEY" not in caplog.text


def test_memory_cache_is_lru_bounded_and_expiry_uses_monotonic_clock(monkeypatch):
    now = [100.0]
    monkeypatch.setattr("app.core.cache.time.monotonic", lambda: now[0])
    c = TTLCache(ttl_seconds=5, max_entries=2)
    c.set("a", 1)
    c.set("b", 2)
    assert c.get("a") == 1
    c.set("c", 3)
    assert c.get("b") is None and c.get("a") == 1
    now[0] = 105
    assert c.get("a") is None
    assert c.stats() == dict(total_entries=0, live_entries=0)
    for i in range(100):
        c.set(str(i), i)
    assert c.stats()["total_entries"] == 2


def test_redis_cache_bounds_io_scans_and_redacts_dependency_errors(caplog):
    with patch("app.core.redis_cache.redis_lib.Redis.from_url") as create:
        c = RedisCache()
        assert create.call_args.kwargs["max_connections"] == 16
        assert create.call_args.kwargs["socket_connect_timeout"] == 2
        assert create.call_args.kwargs["socket_timeout"] == 2
    c._client = MagicMock()
    c._client.get.side_effect = RuntimeError("SECRET_IN_REDIS_ERROR")
    assert c.get("customer-specific-key") is None
    assert (
        "SECRET_IN_REDIS_ERROR" not in caplog.text
        and "customer-specific-key" not in caplog.text
    )
    c._client.scan.return_value = (1, ["a"])
    c.invalidate_prefix("")
    assert c._client.scan.call_count == 100
    c._client.scan.reset_mock()
    assert c.stats()["truncated"] is True
    assert c._client.scan.call_count == 100
    json.dumps(c.stats())

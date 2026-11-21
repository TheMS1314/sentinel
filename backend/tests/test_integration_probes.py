"""Contract reporting must never turn mocks, missing keys or live payment keys green."""

from unittest.mock import patch
from app.ops.verify_integrations import probe


def test_unconfigured_and_no_opt_in_are_blocked(monkeypatch):
    assert probe("model")["status"] == "blocked"
    monkeypatch.setenv("OPENROUTER_API_KEY", "")
    assert probe("model", True)["status"] == "blocked"
    monkeypatch.setenv("RAZORPAY_KEY_ID", "")
    assert probe("razorpay_test", True)["status"] == "blocked"
    monkeypatch.setenv("RAZORPAY_KEY_ID", "rzp_live_prohibited")
    monkeypatch.setenv("RAZORPAY_KEY_SECRET", "not-a-real-secret")
    assert probe("razorpay_test", True)["status"] == "failed"


def test_mock_usage_cannot_satisfy_live_contract(monkeypatch):
    monkeypatch.setenv("LLM_MODEL", "test-model")
    with patch("app.core.llm_client.LLMClient") as client:
        client.return_value.mock_mode = False
        client.return_value.complete_json.return_value = {
            "verdict": "HOLD",
            "_usage": {"mode": "mock"},
        }
        assert probe("model", True)["status"] == "failed"


def test_provider_exception_report_does_not_leak_response_or_token(monkeypatch):
    monkeypatch.setenv("SENTINEL_PROBE_DOMAIN_ID", "idn:example.com")
    with patch(
        "app.core.providers.fetch",
        side_effect=RuntimeError("sensitive-response-and-token"),
    ):
        report = probe("recorded_future", True)
        assert report["status"] == "failed" and report["failure_type"] == "RuntimeError"
        assert "sensitive" not in str(report)

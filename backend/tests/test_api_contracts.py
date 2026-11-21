"""
Phase 2 API contract tests
============================
Covers the additions made during the API/data-contracts stabilization
pass: paginated /transactions, the N+1 fix (asserted via query count,
not just correctness), batch job tracking, and the global error
envelope. Uses FastAPI's TestClient against a real Postgres test
database (same approach as the rest of the suite) rather than mocking
the DB layer, since the whole point of these tests is to catch
contract-shape regressions that a mocked session would hide.
"""

from __future__ import annotations
import os
import pytest
from fastapi.testclient import TestClient

os.environ.setdefault("SENTINEL_API_KEY", "test-key-contracts")
os.environ.setdefault("LLM_PROVIDER", "openrouter")
os.environ.setdefault("OPENROUTER_API_KEY", "")
# This suite legitimately makes far more requests per second than any
# real client would (tight polling loops for job/batch status), so it
# needs its own generous rate-limit ceiling -- separate from the
# defaults a real deployment should keep tight. Set before import since
# rate_limit.py reads this at call time, not at import time, but we set
# it up front for clarity and to avoid any import-order surprises.
os.environ.setdefault("SENTINEL_RATE_LIMIT_REQUESTS", "5000")
os.environ.setdefault("SENTINEL_RATE_LIMIT_WINDOW_SECONDS", "60")

from app.api.main import app  # noqa: E402
from app.models.db import (
    Base,
    engine,
    SessionLocal,
    PolicyRecord,
    TransactionRecord,
    DecisionRecord,
    PaymentRecord,
    ReviewEvent,
)  # noqa: E402

HEADERS = {"X-API-Key": "test-key-contracts"}


@pytest.fixture(autouse=True)
def clean_db():
    Base.metadata.create_all(bind=engine)
    db = SessionLocal()
    try:
        db.query(ReviewEvent).delete()
        db.query(PaymentRecord).delete()
        db.query(DecisionRecord).delete()
        db.query(TransactionRecord).delete()
        db.query(PolicyRecord).delete()
        db.commit()
    finally:
        db.close()
    yield


@pytest.fixture(scope="module")
def client():
    """Module-scoped, not function-scoped: the eval queue is a
    module-level singleton bound to the event loop active when it's
    first started. Entering/exiting TestClient's `with` block per test
    would start/stop it against a *different* loop each time (the real
    app only ever starts it once, in one long-lived loop), which
    silently stalls the queue on the second test -- a test-harness
    mismatch with production lifecycle, not a bug in the queue itself.
    """
    with TestClient(app) as c:
        yield c


def _seed_policy(db, agent_id="agent_x"):
    db.add(
        PolicyRecord(
            policy_id=f"pol_{agent_id}",
            agent_id=agent_id,
            owner_human_id="human_x",
            version=1,
            active=True,
            policy_json={
                "policy_id": f"pol_{agent_id}",
                "agent_id": agent_id,
                "owner_human_id": "human_x",
                "max_amount_per_txn": 2000.0,
                "max_daily_total": 5000.0,
                "allowed_categories": ["grocery"],
                "blocked_merchants": [],
                "allowed_merchants": None,
                "time_window": {"start_hour": 6, "end_hour": 22},
                "velocity": {"max_count": 3, "window_minutes": 1440},
                "active": True,
                "version": 1,
            },
        )
    )


def _seed_transactions(db, n=7, agent_id="agent_x"):
    for i in range(n):
        tx = TransactionRecord(
            request_id=f"req_{agent_id}_{i}",
            agent_id=agent_id,
            merchant_name=f"Merchant{i}",
            merchant_category="grocery",
            amount=float(10 + i),
            requested_at_hour=10,
            requested_at_iso=f"2026-08-{10 + i:02d}T10:00:00",
            stated_intent="buy stuff",
            scenario_tag="test",
        )
        db.add(tx)
        db.flush()
        db.add(
            DecisionRecord(
                request_id=tx.request_id,
                verdict="BLOCK" if i % 3 == 0 else "APPROVE",
                reasons=["r"],
                checked_rules=["r"],
                decided_by="rule_engine",
            )
        )


class TestPagination:
    def test_page_mode_returns_envelope_with_metadata(self, client):
        db = SessionLocal()
        _seed_transactions(db, n=7)
        db.commit()
        db.close()

        resp = client.get("/transactions?page=1&page_size=3", headers=HEADERS)
        assert resp.status_code == 200
        body = resp.json()
        assert set(body.keys()) == {
            "items",
            "page",
            "page_size",
            "total",
            "total_pages",
        }
        assert body["total"] == 7
        assert body["page_size"] == 3
        assert len(body["items"]) == 3
        assert body["total_pages"] == 3

    def test_page_two_returns_remaining_distinct_items(self, client):
        db = SessionLocal()
        _seed_transactions(db, n=7)
        db.commit()
        db.close()

        page1 = client.get("/transactions?page=1&page_size=5", headers=HEADERS).json()
        page2 = client.get("/transactions?page=2&page_size=5", headers=HEADERS).json()
        ids1 = {t["request_id"] for t in page1["items"]}
        ids2 = {t["request_id"] for t in page2["items"]}
        assert len(page2["items"]) == 2
        assert ids1.isdisjoint(ids2)

    def test_legacy_limit_mode_still_returns_bare_list(self, client):
        """The pre-pagination frontend contract must keep working."""
        db = SessionLocal()
        _seed_transactions(db, n=5)
        db.commit()
        db.close()

        resp = client.get("/transactions?limit=2", headers=HEADERS)
        assert resp.status_code == 200
        body = resp.json()
        assert isinstance(body, list)
        assert len(body) == 2

    def test_verdict_filter(self, client):
        db = SessionLocal()
        _seed_transactions(db, n=6)  # i=0,3 -> BLOCK (2 of them)
        db.commit()
        db.close()

        resp = client.get(
            "/transactions?page=1&page_size=50&verdict=BLOCK", headers=HEADERS
        )
        body = resp.json()
        assert body["total"] == 2
        assert all(t["verdict"] == "BLOCK" for t in body["items"])

    def test_requested_and_processed_timestamps_both_exposed_and_distinct(self, client):
        """Regression test for brief section 26: requested_at_iso is
        the (often deterministic, scenario-generated) event time --
        showing only that in the UI made every transaction look
        dated to whatever fixed reference date the scenario used,
        regardless of when the demo was actually run. created_at_iso
        is the real DB persist time and must be exposed separately,
        and must be UTC-offset-aware (not a naive datetime string)."""
        db = SessionLocal()
        _seed_transactions(db, n=1)
        db.commit()
        db.close()

        resp = client.get("/transactions?page=1&page_size=1", headers=HEADERS)
        item = resp.json()["items"][0]
        assert "requested_at_iso" in item
        assert "created_at_iso" in item
        assert item["created_at_iso"] is not None
        # offset-aware: must end in +00:00 or Z, not a bare naive string
        assert item["created_at_iso"].endswith("+00:00") or item[
            "created_at_iso"
        ].endswith("Z")

    def test_search_filter_matches_merchant_name(self, client):
        db = SessionLocal()
        _seed_transactions(db, n=3)
        db.commit()
        db.close()

        resp = client.get(
            "/transactions?page=1&page_size=50&search=Merchant1", headers=HEADERS
        )
        body = resp.json()
        assert body["total"] == 1
        assert body["items"][0]["merchant_name"] == "Merchant1"

    def test_no_n_plus_1_query_for_decisions(self, client):
        """The original implementation issued one extra query per
        transaction row to fetch its decision. This asserts the fixed
        query count stays flat as row count grows -- a correctness
        check alone wouldn't catch a regression back to N+1."""
        from sqlalchemy import event

        db = SessionLocal()
        _seed_transactions(db, n=20)
        db.commit()
        db.close()

        queries = []

        def counter(conn, cursor, statement, *args):
            queries.append(statement)

        event.listen(engine, "before_cursor_execute", counter)
        try:
            resp = client.get("/transactions?page=1&page_size=20", headers=HEADERS)
        finally:
            event.remove(engine, "before_cursor_execute", counter)

        assert resp.status_code == 200
        select_queries = [q for q in queries if q.strip().upper().startswith("SELECT")]
        # One count query + one joined select -- not 1 + 20.
        assert len(select_queries) <= 3, (
            f"expected O(1) queries, got {len(select_queries)}: {select_queries}"
        )

    def test_page_beyond_final_page_returns_empty_items_not_error(self, client):
        db = SessionLocal()
        _seed_transactions(db, n=3)
        db.commit()
        db.close()

        resp = client.get("/transactions?page=99&page_size=10", headers=HEADERS)
        assert resp.status_code == 200
        body = resp.json()
        assert body["items"] == []
        assert body["total"] == 3

    def test_invalid_page_zero_rejected(self, client):
        resp = client.get("/transactions?page=0&page_size=10", headers=HEADERS)
        assert resp.status_code == 422
        assert "error" in resp.json()

    def test_invalid_negative_page_rejected(self, client):
        resp = client.get("/transactions?page=-1&page_size=10", headers=HEADERS)
        assert resp.status_code == 422

    def test_invalid_page_size_zero_rejected(self, client):
        resp = client.get("/transactions?page=1&page_size=0", headers=HEADERS)
        assert resp.status_code == 422

    def test_invalid_page_size_too_large_rejected(self, client):
        resp = client.get("/transactions?page=1&page_size=100000", headers=HEADERS)
        assert resp.status_code == 422

    def test_empty_result_set_paginates_cleanly(self, client):
        """No seeded data at all -- total=0, total_pages must still be
        >=1 (not 0, which would make 'page 1 of 0' nonsensical UI)."""
        resp = client.get("/transactions?page=1&page_size=10", headers=HEADERS)
        body = resp.json()
        assert body["total"] == 0
        assert body["items"] == []
        assert body["total_pages"] >= 1

    def test_golden_eval_transactions_excluded_from_live_feed(self, client):
        """Regression test for a real bug found in this pass: running
        the eval harness persists golden_eval/golden_history-tagged
        transactions into the same table the Live Feed reads from
        (needed so velocity-dependent golden cases have real history
        to score against). Confirmed live: transaction count went from
        18 to 153 after running the harness once. The Live Feed API
        must never surface those rows, even though they're genuinely
        in the database."""
        db = SessionLocal()
        _seed_transactions(db, n=2, agent_id="agent_live")
        # Simulate what eval_harness.py actually persists.
        for i, tag in enumerate(["golden_eval", "golden_history"]):
            tx = TransactionRecord(
                request_id=f"req_golden_{i}",
                agent_id="agent_eval_internal",
                merchant_name="GoldenMerchant",
                merchant_category="grocery",
                amount=99.0,
                requested_at_hour=10,
                requested_at_iso="2026-08-01T10:00:00",
                stated_intent="golden case",
                scenario_tag=tag,
            )
            db.add(tx)
            db.flush()
            db.add(
                DecisionRecord(
                    request_id=tx.request_id,
                    verdict="APPROVE",
                    reasons=["r"],
                    checked_rules=["r"],
                    decided_by="rule_engine",
                )
            )
        db.commit()
        db.close()

        resp = client.get("/transactions?page=1&page_size=50", headers=HEADERS)
        body = resp.json()
        assert body["total"] == 2, (
            f"golden-tagged rows leaked into the live feed: total={body['total']}"
        )
        assert all(not t["request_id"].startswith("req_golden_") for t in body["items"])

        scenarios = client.get("/transactions/scenarios", headers=HEADERS).json()
        assert "golden_eval" not in scenarios
        assert "golden_history" not in scenarios

        summary = client.get("/transactions/summary", headers=HEADERS).json()
        assert summary["total"] == 2, (
            f"golden-tagged rows leaked into summary counts: total={summary['total']}"
        )


class TestSummary:
    def test_summary_counts_by_verdict(self, client):
        db = SessionLocal()
        _seed_transactions(db, n=6)  # i=0,3 BLOCK; i=1,2,4,5 APPROVE
        db.commit()
        db.close()

        resp = client.get("/transactions/summary", headers=HEADERS)
        body = resp.json()
        assert body["total"] == 6
        assert body["by_verdict"]["BLOCK"] == 2
        assert body["by_verdict"]["APPROVE"] == 4

    def test_summary_scoped_to_agent_id(self, client):
        """A multi-agent dashboard's stats bar must match whichever
        agent the feed is currently filtered to -- an unscoped summary
        next to an agent-scoped feed would show mismatched totals."""
        db = SessionLocal()
        _seed_transactions(db, n=3, agent_id="agent_alpha")
        _seed_transactions(db, n=5, agent_id="agent_beta")
        db.commit()
        db.close()

        alpha = client.get(
            "/transactions/summary?agent_id=agent_alpha", headers=HEADERS
        ).json()
        beta = client.get(
            "/transactions/summary?agent_id=agent_beta", headers=HEADERS
        ).json()
        both = client.get("/transactions/summary", headers=HEADERS).json()

        assert alpha["total"] == 3
        assert beta["total"] == 5
        assert both["total"] == 8


class TestBatchJobs:
    def test_batch_evaluate_and_status_roundtrip(self, client):
        db = SessionLocal()
        _seed_policy(db, agent_id="agent_batch")
        db.commit()
        db.close()

        payload = {
            "requests": [
                {
                    "agent_id": "agent_batch",
                    "merchant_name": "FreshMart",
                    "merchant_category": "grocery",
                    "amount": 20,
                    "requested_at_hour": 10,
                    "requested_at_iso": "2026-09-01T10:00:00",
                    "stated_intent": "buy groceries",
                }
                for _ in range(3)
            ]
        }
        resp = client.post(
            "/transactions/evaluate-batch", json=payload, headers=HEADERS
        )
        assert resp.status_code == 200
        batch_id = resp.json()["batch_id"]
        assert len(resp.json()["job_ids"]) == 3

        import time

        deadline = time.monotonic() + 10
        status = None
        while time.monotonic() < deadline:
            status = client.get(f"/jobs/batch/{batch_id}", headers=HEADERS).json()
            if status["complete"]:
                break
            time.sleep(0.5)

        if status is None or not status["complete"]:
            for jid in resp.json()["job_ids"]:
                print("JOB", jid, client.get(f"/jobs/{jid}", headers=HEADERS).json())
        assert status is not None and status["complete"] is True, (
            f"batch never completed: {status}"
        )
        assert status["total"] == 3
        assert status["by_status"]["DONE"] == 3

    def test_empty_batch_rejected(self, client):
        resp = client.post(
            "/transactions/evaluate-batch", json={"requests": []}, headers=HEADERS
        )
        assert resp.status_code == 422
        assert "error" in resp.json()

    def test_unknown_batch_returns_404_envelope(self, client):
        resp = client.get("/jobs/batch/nonexistent", headers=HEADERS)
        assert resp.status_code == 404
        assert resp.json()["error"]["status_code"] == 404


class TestErrorEnvelope:
    def test_missing_api_key_returns_error_envelope(self, client):
        resp = client.get("/transactions")
        assert resp.status_code == 401
        assert "error" in resp.json()
        assert "message" in resp.json()["error"]

    def test_404_returns_error_envelope(self, client):
        resp = client.get("/policies/does_not_exist", headers=HEADERS)
        assert resp.status_code == 404
        assert resp.json()["error"]["message"]

    def test_validation_error_returns_error_envelope(self, client):
        # missing required 'agent_id' field
        resp = client.post("/policies", json={"owner_human_id": "h"}, headers=HEADERS)
        assert resp.status_code == 422
        assert resp.json()["error"]["status_code"] == 422

    def test_simulate_num_transactions_upper_bound_rejected(self, client):
        """Regression test: num_transactions had no server-side bound,
        so a client could force a synchronous PurchasingAgent run of
        arbitrary size. The frontend's min/max on the number input
        never enforced this -- those are cosmetic HTML attributes, not
        a real boundary."""
        resp = client.post(
            "/simulate-async",
            json={"agent_id": "agent_x", "num_transactions": 9999},
            headers=HEADERS,
        )
        assert resp.status_code == 422
        assert resp.json()["error"]["status_code"] == 422

    def test_simulate_num_transactions_zero_rejected(self, client):
        resp = client.post(
            "/simulate-async",
            json={"agent_id": "agent_x", "num_transactions": 0},
            headers=HEADERS,
        )
        assert resp.status_code == 422


class TestHealth:
    def test_health_reports_database_check(self, client):
        resp = client.get("/health")
        assert resp.status_code == 200
        body = resp.json()
        assert body["checks"]["database"] == "ok"
        assert body["status"] == "ok"

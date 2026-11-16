"""Isolated mock pipeline benchmark. This is not an HTTP/deployment capacity test."""

from __future__ import annotations
import argparse
import asyncio
import json
import os
from pathlib import Path
import platform
import statistics
import tempfile
import time
import uuid


async def run_load_test(num_requests, num_workers, num_agents=1):
    if (
        not 1 <= num_requests <= 500
        or not 1 <= num_agents <= 100
        or not 1 <= num_workers <= 32
    ):
        raise ValueError("requests 1..500, agents 1..100, workers 1..32")
    os.environ.update(
        SENTINEL_ENV="evaluation",
        LLM_PROVIDER="openrouter",
        OPENROUTER_API_KEY="",
        ANTHROPIC_API_KEY="",
        RAZORPAY_KEY_ID="",
        RAZORPAY_KEY_SECRET="",
        QUEUE_BACKEND="memory",
        SENTINEL_DATABASE_URL="sqlite:///"
        + tempfile.mktemp(prefix="sentinel-load-", suffix=".sqlite"),
    )
    from app.models.db import init_db, SessionLocal, PolicyRecord
    from app.models.policy import AgentPolicy, TimeWindow, VelocityLimit
    from app.core.job_queue import EvaluationQueue
    from app.api.main import _process_single_evaluation

    init_db()
    db = SessionLocal()
    agents = ["load_" + uuid.uuid4().hex for _ in range(num_agents)]
    for agent in agents:
        p = AgentPolicy(
            "pol_" + agent,
            agent,
            "load-owner",
            max_daily_total=10_000_000,
            time_window=TimeWindow(0, 23),
            velocity=VelocityLimit(100000, 1440),
        )
        db.add(
            PolicyRecord(
                policy_id=p.policy_id,
                agent_id=agent,
                owner_human_id=p.owner_human_id,
                version=1,
                active=True,
                policy_json=p.to_dict(),
            )
        )
    db.commit()
    db.close()
    queue = EvaluationQueue(_process_single_evaluation, num_workers=num_workers)
    await queue.start()
    start = time.perf_counter()
    ids = []
    for i in range(num_requests):
        ids.append(
            await queue.enqueue(
                dict(
                    request_id="load_" + uuid.uuid4().hex,
                    agent_id=agents[i % num_agents],
                    merchant_name="FreshMart",
                    merchant_category="grocery",
                    amount=5000 if i % 10 >= 8 else 300,
                    requested_at_hour=14,
                    requested_at_iso="2026-08-24T14:00:00",
                    stated_intent="restock kitchen supplies"
                    if i % 10 == 7
                    else "buy weekly groceries",
                    raw_agent_goal="buy weekly groceries under budget",
                    scenario_tag="loadtest",
                )
            )
        )
    remaining = set(ids)
    durations = []
    while remaining and time.perf_counter() - start < 60:
        await asyncio.sleep(0.02)
        for jid in list(remaining):
            job = queue.get_job(jid)
            if job.status.value in ("DONE", "FAILED"):
                if job.status.value == "DONE":
                    durations.append(time.perf_counter() - start)
                remaining.remove(jid)
    elapsed = time.perf_counter() - start
    jobs = [queue.get_job(jid) for jid in ids]
    await queue.stop()
    done = sum(j.status.value == "DONE" for j in jobs)
    failed = sum(j.status.value == "FAILED" for j in jobs)
    report = {
        "generated_at": __import__("datetime")
        .datetime.now(__import__("datetime").timezone.utc)
        .isoformat(),
        "runtime": {
            "python": platform.python_version(),
            "platform": platform.platform(),
            "llm_mode": "mock",
            "gateway_mode": "mock",
            "queue": "memory",
            "database": "isolated SQLite",
        },
        "scope": "Pipeline microbenchmark; excludes HTTP/nginx/shared Redis and production hardware.",
        "num_requests": num_requests,
        "num_workers": num_workers,
        "num_agents": num_agents,
        "successful": done,
        "failed": failed,
        "pending": len(remaining),
        "total_wall_seconds": elapsed,
        "successful_per_second": done / elapsed,
        "observed_completion_ms": {
            "median": statistics.median(durations) * 1000 if durations else None
        },
    }
    return report


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--requests", type=int, default=100)
    parser.add_argument("--workers", type=int, default=4)
    parser.add_argument("--agents", type=int, default=10)
    args = parser.parse_args()
    report = asyncio.run(run_load_test(args.requests, args.workers, args.agents))
    target = (
        Path(__file__).resolve().parents[2]
        / "data"
        / f"loadtest_report_{args.agents}agents.json"
    )
    target.write_text(json.dumps(report, indent=2) + "\n")
    print(json.dumps(report, indent=2))

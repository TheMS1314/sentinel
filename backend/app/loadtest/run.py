"""
Load Test
==========
Measures REAL throughput and latency of Sentinel's evaluation pipeline
under concurrent load, via the async job queue -- not an estimate, an
actual measurement, so the numbers quoted in the pitch are real.

Usage:
    python -m app.loadtest.run --requests 200 --workers 8
"""

from __future__ import annotations
import argparse
import asyncio
import time
import statistics
import sys
import os
import uuid

from app.core.env import load_env
load_env()  # must run before any app import below reads os.environ

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))


def build_payload(i: int, agent_id: str) -> dict:
    bucket = i % 10
    if bucket < 6:
        amount, category, intent = 300.0 + (i % 50) * 5, "grocery", "buy groceries"
    elif bucket < 8:
        amount, category, intent = 5000.0, "grocery", "buy groceries"  # will BLOCK on amount cap
    else:
        amount, category, intent = 400.0, "grocery", "restock kitchen supplies"  # will ESCALATE

    return {
        "request_id": f"loadtest_{uuid.uuid4().hex[:10]}",
        "agent_id": agent_id,
        "merchant_name": "FreshMart",
        "merchant_category": category,
        "amount": amount,
        "requested_at_hour": 14,
        "requested_at_iso": "2026-08-24T14:00:00",
        "stated_intent": intent,
        "raw_agent_goal": "buy weekly groceries under budget",
        "scenario_tag": "loadtest",
    }


async def run_load_test(num_requests: int, num_workers: int, num_agents: int = 1):
    """num_agents=1 is a deliberate worst-case stress test: all load
    hits one agent's growing history, so every later request pays for
    more velocity/daily-total/RAG lookups than the one before it.
    num_agents>1 spreads load the way real usage actually would -- many
    agents, each with much smaller individual history -- and gives a
    more representative throughput number. Reporting both, not just
    whichever looks better, is the honest way to present this."""
    os.environ.setdefault("OPENROUTER_API_KEY", "")

    from app.models.db import init_db, PolicyRecord, SessionLocal
    from app.models.policy import AgentPolicy, TimeWindow, VelocityLimit
    from app.core.job_queue import EvaluationQueue
    from app.api.main import _process_single_evaluation

    init_db()
    db = SessionLocal()
    agent_ids = [f"agent_loadtest_{uuid.uuid4().hex[:6]}" for _ in range(num_agents)]
    for agent_id in agent_ids:
        policy = AgentPolicy(
            policy_id=f"pol_{agent_id}", agent_id=agent_id, owner_human_id="human_loadtest",
            max_amount_per_txn=2000.0, max_daily_total=10_000_000.0,
            allowed_categories=["grocery"], time_window=TimeWindow(0, 23), velocity=VelocityLimit(10_000, 1440),
        )
        db.add(PolicyRecord(
            policy_id=policy.policy_id, agent_id=agent_id, owner_human_id="human_loadtest",
            version=1, active=True, policy_json=policy.to_dict(),
        ))
    db.commit()
    db.close()

    queue = EvaluationQueue(submit_fn=_process_single_evaluation, num_workers=num_workers)
    await queue.start()

    payloads = [build_payload(i, agent_ids[i % num_agents]) for i in range(num_requests)]

    submit_start = time.perf_counter()
    job_ids = []
    per_job_submit_time = {}
    for p in payloads:
        t0 = time.perf_counter()
        job_id = await queue.enqueue(p)
        per_job_submit_time[job_id] = t0
        job_ids.append(job_id)
    submit_end = time.perf_counter()

    latencies = []
    pending = set(job_ids)
    poll_start = time.perf_counter()
    while pending:
        await asyncio.sleep(0.02)
        done_now = []
        for jid in pending:
            job = queue.get_job(jid)
            if job.status.value in ("DONE", "FAILED"):
                latencies.append(time.perf_counter() - per_job_submit_time[jid])
                done_now.append(jid)
        for jid in done_now:
            pending.discard(jid)
        if time.perf_counter() - poll_start > 60:
            print(f"WARNING: {len(pending)} jobs still pending after 60s, stopping poll.")
            break

    total_end = time.perf_counter()
    await queue.stop()

    latencies.sort()
    n = len(latencies)

    def pctile(p):
        if not n:
            return float("nan")
        idx = min(int(p / 100 * n), n - 1)
        return latencies[idx]

    total_wall = total_end - submit_start
    throughput = n / total_wall if total_wall > 0 else float("nan")

    print("=" * 60)
    print(f"SENTINEL LOAD TEST -- {num_requests} requests, {num_workers} workers, {num_agents} agent(s)")
    print("=" * 60)
    print(f"Submitted:        {num_requests} requests in {submit_end - submit_start:.3f}s "
          f"({num_requests / (submit_end - submit_start):.1f} submits/sec -- non-blocking enqueue)")
    print(f"Completed:        {n}/{num_requests}")
    print(f"Total wall time:  {total_wall:.3f}s")
    print(f"Throughput:       {throughput:.1f} evaluations/sec (end-to-end, includes queue wait + processing)")
    print(f"Latency (submit->done):")
    print(f"  p50: {pctile(50)*1000:.1f}ms")
    print(f"  p90: {pctile(90)*1000:.1f}ms")
    print(f"  p95: {pctile(95)*1000:.1f}ms")
    print(f"  p99: {pctile(99)*1000:.1f}ms")
    print(f"  max: {latencies[-1]*1000:.1f}ms" if latencies else "  max: n/a")
    print(f"  mean: {statistics.mean(latencies)*1000:.1f}ms" if latencies else "  mean: n/a")

    return {
        "num_requests": num_requests,
        "num_workers": num_workers,
        "completed": n,
        "total_wall_seconds": total_wall,
        "throughput_per_sec": throughput,
        "latency_ms": {
            "p50": pctile(50) * 1000, "p90": pctile(90) * 1000,
            "p95": pctile(95) * 1000, "p99": pctile(99) * 1000,
            "max": latencies[-1] * 1000 if latencies else None,
            "mean": statistics.mean(latencies) * 1000 if latencies else None,
        },
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--requests", type=int, default=200)
    parser.add_argument("--workers", type=int, default=8)
    parser.add_argument("--agents", type=int, default=1)
    args = parser.parse_args()

    result = asyncio.run(run_load_test(args.requests, args.workers, args.agents))

    import json
    filename = f"loadtest_report_{args.agents}agents.json"
    out_path = os.path.join(os.path.dirname(__file__), "..", "..", "data", filename)
    out_path = os.path.abspath(out_path)
    with open(out_path, "w") as f:
        json.dump(result, f, indent=2)
    print(f"\nReport saved to {out_path}")

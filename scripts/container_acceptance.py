"""Exercise an isolated Compose stack; never point this at an existing deployment.

CI starts a fresh Compose project. This script changes its demo database and
recreates/stops containers. It requires an explicit acceptance environment flag.
"""

from concurrent.futures import ThreadPoolExecutor
import json
import os
from pathlib import Path
import statistics
import sys
import subprocess
import time
import urllib.error
import urllib.request
import uuid

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))
from app.schema_version import SCHEMA_REVISION

if os.environ.get("SENTINEL_ACCEPTANCE") != "1" or not os.environ.get(
    "COMPOSE_PROJECT_NAME", ""
).startswith("sentinel-ci-"):
    raise SystemExit(
        "Only run against a fresh sentinel-ci-* Compose project with SENTINEL_ACCEPTANCE=1"
    )
BASE = "http://127.0.0.1:8000"
registry = next(
    line.split("=", 1)[1]
    for line in (ROOT / "backend/.env").read_text().splitlines()
    if line.startswith("SENTINEL_CREDENTIALS=")
)
KEY = json.loads(registry)[0]["key"]
report = {
    "scope": "isolated Compose / PostgreSQL / SQL job queue / Redis quota / mock providers",
    "checks": {},
    "timings": {},
}


def compose(*args, input=None):
    return subprocess.run(
        ["docker", "compose", *args],
        cwd=ROOT,
        input=input,
        capture_output=True,
        check=True,
    ).stdout


def http(path, body=None, method=None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(
        BASE + path,
        data=data,
        method=method,
        headers={
            "X-API-Key": KEY,
            "Content-Type": "application/json",
            "Origin": "http://127.0.0.1:8000",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=40) as response:
            raw = response.read()
            return response.status, json.loads(raw) if raw.startswith(
                b"{"
            ) or raw.startswith(b"[") else raw
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read()


def wait_ready(timeout=120):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        try:
            if http("/health")[0] == 200:
                return
        except (OSError, urllib.error.URLError):
            pass
        time.sleep(1)
    raise AssertionError("Stack did not become ready")


def check(name, condition):
    if not condition:
        raise AssertionError(name)
    report["checks"][name] = "pass"
    print(name + ": pass", flush=True)


wait_ready()
check("built_frontend", http("/")[0] == 200)
agent = "ci_" + uuid.uuid4().hex
policy = {
    "agent_id": agent,
    "owner_human_id": "ci-owner",
    "authorized_goal": "buy weekly groceries",
    "max_amount_per_txn": 1000,
    "max_daily_total": 100000,
    "velocity_max_count": 1000,
    "time_window_start": 0,
    "time_window_end": 23,
}
check("policy_create", http("/api/policies", policy)[0] == 200)


def purchase(rid=None):
    return {
        "request_id": rid or "req_" + uuid.uuid4().hex,
        "agent_id": agent,
        "merchant_name": "AcceptanceShop",
        "merchant_category": "grocery",
        "amount": 1,
        "stated_intent": "buy weekly groceries",
    }


req = purchase()
check("approve", http("/api/transactions/evaluate", req)[1]["verdict"] == "APPROVE")
check("replay", http("/api/transactions/evaluate", req)[1]["verdict"] == "APPROVE")
check(
    "changed_replay_conflicts",
    http("/api/transactions/evaluate", dict(req, amount=2))[0] == 409,
)
check(
    "hard_cap",
    http("/api/transactions/evaluate", dict(purchase(), amount=1001))[1]["verdict"]
    == "BLOCK",
)


# Merchant attributes are server-owned once a stable identity is supplied.
registered = http(
    "/api/merchants",
    {
        "owner_id": "ci-owner",
        "legal_name": "RegisteredCasino",
        "category": "casino",
        "domain": "casino.example",
        "operator_reference": "Synthetic CI operator registration",
        "expected_version": 0,
    },
)
check("merchant_directory_registration", registered[0] == 200)
spoofed = dict(
    purchase(),
    merchant_id=registered[1]["merchant_id"],
    merchant_name="Groceries",
    merchant_category="grocery",
)
check(
    "merchant_category_cannot_be_spoofed",
    http("/api/transactions/evaluate", spoofed)[1]["verdict"] == "BLOCK",
)


# Capture actual HTTP throughput scope and both success/failure counts.
def timed(_):
    start = time.perf_counter()
    status, _body = http("/api/transactions/evaluate", purchase())
    return status, (time.perf_counter() - start) * 1000


start = time.perf_counter()
with ThreadPoolExecutor(max_workers=8) as pool:
    results = list(pool.map(timed, range(24)))
elapsed = time.perf_counter() - start
latencies = sorted(t for _, t in results)
report["timings"]["http_mock"] = {
    "requests": len(results),
    "workers": 8,
    "successful": sum(s == 200 for s, _ in results),
    "failed": sum(s != 200 for s, _ in results),
    "elapsed_seconds": round(elapsed, 3),
    "throughput_per_second": round(len(results) / elapsed, 2),
    "p50_ms": round(statistics.median(latencies), 2),
    "p95_ms": round(latencies[int(0.95 * (len(latencies) - 1))], 2),
}
check("http_concurrency", all(s == 200 for s, _ in results))

# Shared authority is independent of per-replica environment snapshots.
created = http(
    "/api/credentials",
    {
        "subject": "ci-agent",
        "action": "create",
        "expected_version": 0,
        "role": "agent",
        "agent_ids": [agent],
    },
)
check("shared_credential_issue", created[0] == 200)
agent_key = created[1]["credential"]


def scoped_me(key):
    req = urllib.request.Request(BASE + "/api/auth/me", headers={"X-API-Key": key})
    try:
        with urllib.request.urlopen(req, timeout=10) as response:
            return response.status
    except urllib.error.HTTPError as exc:
        return exc.code


check(
    "shared_credential_authentication",
    all(scoped_me(agent_key) == 200 for _ in range(4)),
)
check(
    "shared_credential_revoke",
    http(
        "/api/credentials",
        {"subject": "ci-agent", "action": "revoke", "expected_version": 1},
    )[0]
    == 200,
)
check(
    "revocation_reaches_all_replicas",
    all(scoped_me(agent_key) == 401 for _ in range(4)),
)

# Freeze at dispatch, then cancel the pending reservation before resuming.
state = http("/api/risk/overview")[1]
body = {
    "expected_version": state["guard"]["version"],
    "frozen": True,
    "reason": "CI freeze test",
}
check("freeze", http("/api/risk/guard", body)[0] == 200)
paused = purchase()
check(
    "frozen_approval",
    http("/api/transactions/evaluate", paused)[1]["verdict"] == "APPROVE",
)
check(
    "cancel_undispatched",
    http(
        "/api/payments/" + paused["request_id"] + "/cancel",
        {"reason": "CI cancel before unfreeze"},
    )[1]["status"]
    == "cancelled",
)
body.update(
    expected_version=body["expected_version"] + 1, frozen=False, reason="CI resume"
)
check("unfreeze", http("/api/risk/guard", body)[0] == 200)

# Accepted jobs live in PostgreSQL, so stopping Redis does not erase their work.
queued = http(
    "/api/transactions/evaluate-batch", {"requests": [purchase() for _ in range(12)]}
)
check("durable_batch_accepted", queued[0] == 200)
compose("stop", "redis")
try:
    check(
        "quota_outage_fails_closed",
        http("/api/transactions/evaluate", purchase())[0] == 503,
    )
    check("liveness_survives_quota_outage", http("/api/health/live")[0] == 200)
    check("readiness_reports_quota_outage", http("/health")[0] == 503)
finally:
    compose("start", "redis")
wait_ready()
for _ in range(100):
    status, batch = http("/api/jobs/batch/" + queued[1]["batch_id"])
    if status == 200 and batch["complete"]:
        break
    time.sleep(0.2)
check(
    "accepted_jobs_survive_redis_outage",
    status == 200 and batch["by_status"].get("DONE") == 12,
)

# Database interruption must fail closed while liveness remains available.
compose("stop", "postgres")
try:
    check(
        "database_outage_fails_closed",
        http("/api/transactions/evaluate", purchase())[0] == 503,
    )
    check("liveness_survives_database_outage", http("/api/health/live")[0] == 200)
    check("readiness_reports_database_outage", http("/health")[0] == 503)
finally:
    compose("start", "postgres")
wait_ready()
check(
    "database_restart_recovery",
    http("/api/transactions/evaluate", purchase())[0] == 200,
)

# Recreate one backend to change its container IP. Resolver must refresh and the
# surviving backend must continue serving requests without a gateway restart.
compose("stop", "backend1")
check("one_backend_down", http("/api/transactions/evaluate", purchase())[0] == 200)
compose("up", "-d", "--force-recreate", "backend1")
wait_ready()
seen = set()
for _ in range(40):
    status, data = http("/health")
    if status == 200:
        seen.add(data["instance_id"])
    if len(seen) == 2:
        break
    time.sleep(0.5)
check("backend_recreation_dns", seen == {"backend1", "backend2"})

# Restore a logical backup into a new throwaway database, never over the source.
start = time.perf_counter()
dump = compose(
    "exec", "-T", "postgres", "pg_dump", "-U", "sentinel", "-d", "sentinel", "-Fc"
)
compose("exec", "-T", "postgres", "createdb", "-U", "sentinel", "sentinel_restore_test")
try:
    compose(
        "exec",
        "-T",
        "postgres",
        "pg_restore",
        "-U",
        "sentinel",
        "-d",
        "sentinel_restore_test",
        "--no-owner",
        input=dump,
    )
    restored = (
        compose(
            "exec",
            "-T",
            "postgres",
            "psql",
            "-U",
            "sentinel",
            "-d",
            "sentinel_restore_test",
            "-Atc",
            "SELECT COUNT(*) FROM transactions",
        )
        .decode()
        .strip()
    )
    revision = (
        compose(
            "exec",
            "-T",
            "postgres",
            "psql",
            "-U",
            "sentinel",
            "-d",
            "sentinel_restore_test",
            "-Atc",
            "SELECT version_num FROM alembic_version",
        )
        .decode()
        .strip()
    )
    check(
        "backup_restore_records_and_schema",
        int(restored) > 20 and revision == SCHEMA_REVISION,
    )
finally:
    compose(
        "exec", "-T", "postgres", "dropdb", "-U", "sentinel", "sentinel_restore_test"
    )
report["timings"]["logical_restore_seconds"] = round(time.perf_counter() - start, 3)
# Verify actual signed HTTPS delivery across the container boundary.
case = http(
    "/api/ops/incidents",
    {"request_id": req["request_id"], "summary": "CI synthetic manual review"},
)
check("incident_create", case[0] == 200)
incident_id = case[1]["incident_id"]
end = time.monotonic() + 30
while time.monotonic() < end:
    delivered = [
        a
        for a in http("/api/ops/alerts")[1]
        if a["incident_id"] == incident_id and a["state"] == "delivered"
    ]
    if delivered:
        break
    time.sleep(1)
if not delivered:
    print("ALERT_DIAGNOSTICS", json.dumps(http("/api/ops/alerts")[1]), flush=True)
check("signed_https_alert_delivered", bool(delivered))
sink = compose(
    "exec",
    "-T",
    "alerts",
    "python",
    "-c",
    "import urllib.request,ssl;print(urllib.request.urlopen('https://alerts:8443/',context=ssl.create_default_context(cafile='/certs/server.crt')).read().decode())",
)
check("https_sink_received_unique_event", json.loads(sink)["unique_events"] > 0)
check("protected_operational_metrics", http("/api/ops/metrics")[0] == 200)
report["images"] = compose("images", "--format", "json").decode()
artifacts = ROOT / "artifacts"
artifacts.mkdir(exist_ok=True)
(artifacts / "container-acceptance.json").write_text(
    json.dumps(report, indent=2) + "\n"
)
print("ACCEPTANCE_TIMINGS " + json.dumps(report["timings"], sort_keys=True), flush=True)
print("COMPOSE_ACCEPTANCE_PASS", flush=True)

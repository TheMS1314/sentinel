# Sentinel — Agent Payment Trust Layer

Sentinel is a trust and policy layer that sits between an AI purchasing agent and a payment gateway. Before an agent can cause money movement, Sentinel evaluates the transaction against a human-owned policy, records the request, records the decision, and either approves it for the gateway, holds it for human review, or blocks it.

The key design idea is simple:

> **An AI agent may be autonomous, but its payment authority should remain bounded, explainable, and auditable.**

Sentinel is built for the Razorpay AI Builder Buildathon, Agentic Commerce track.

---

## 1. What problem Sentinel solves

Giving an AI agent standing payment authority creates a failure mode that is different from ordinary fraud: **behavioral drift**.

An agent can remain broadly aligned with its goal while gradually:

- spending beyond a per-transaction or daily cap,
- buying outside an authorized category,
- transacting outside allowed hours,
- producing a suspicious burst of attempts,
- attempting a blocked merchant,
- or following injected instructions that conflict with the human-owned goal.

Sentinel treats the agent's policy as the authority boundary and evaluates each transaction at the moment it is attempted. Uncertainty is deliberately conservative: a genuinely ambiguous request can become `HOLD` instead of being guessed into `APPROVE`.

---

## 2. Core decision model

The decision pipeline is intentionally layered.

```text
AI Purchasing Agent
        |
        | TransactionRequest
        v
+-----------------------------+
| Sentinel                    |
|                             |
| 1. Fetch agent history      |
| 2. Audit-log request        |
| 3. Deterministic rules      |
|      |                      |
|      +--> BLOCK             |
|      +--> HOLD (velocity)  |
|      +--> ESCALATE          |
|      +--> APPROVE           |
|             |
|       escalation only       |
|             v               |
| 4. TF-IDF case retrieval    |
| 5. LLM reasoner             |
|      |                      |
|      +--> APPROVE/HOLD/BLOCK|
|                             |
| 6. Audit-log decision       |
| 7. APPROVE -> payment       |
|    HOLD    -> human review  |
|    BLOCK   -> stop          |
+-----------------------------+
        |
        v
Razorpay test-mode gateway
```

### Why the LLM is not the first layer

A deterministic amount or category violation does not become safer because an LLM was asked to rediscover it. The rule engine is faster, reproducible, auditable, and has no model hallucination risk. The LLM is reserved for cases where the deterministic layer cannot make a sufficiently confident judgment about intent drift.

---

## 3. Policy controls

Each agent has an isolated, versioned policy containing:

| Control | Meaning |
|---|---|
| `max_amount_per_txn` | Maximum amount for one transaction |
| `max_daily_total` | Maximum amount that can count as actual daily spend |
| `allowed_categories` | Category allowlist |
| `blocked_merchants` | Explicit merchant denylist |
| `allowed_merchants` | Optional strict merchant allowlist |
| `time_window` | Allowed local request hours |
| `velocity` | Maximum number of attempts in a rolling time window |
| `active` | Whether the policy version is active |
| `version` | Monotonic policy version |

The demo policy for `agent_demo_1` is:

```text
max_amount_per_txn = ₹2000
max_daily_total    = ₹5000
allowed_categories = ["grocery"]
time_window        = 06:00–22:00
velocity           = 3 attempts / 1440 minutes
```

The policy is data, not executable code. The decision engine reads the policy and applies deterministic checks.

---

## 4. Decision outcomes

### APPROVE

The transaction passes the deterministic policy checks, or an escalated request is judged to be within the authorized goal.

Only an `APPROVE` can proceed toward the Razorpay test-mode gateway.

### HOLD

Used for situations that should not be guessed through:

- a velocity-only breach,
- an unresolved ambiguous intent,
- or another explicitly conservative case.

A `HOLD` exposes human-review actions in the dashboard.

### BLOCK

A hard policy violation stops the transaction. Examples include:

- amount cap exceeded,
- daily cap exceeded,
- unsupported category,
- blocked merchant,
- outside the allowed time window,
- explicit intent/override language that violates the policy boundary,
- or another hard rule failure.

---

## 5. Behavioral risk layer

Sentinel also contains a composite behavioral risk scorer. It combines multiple signals rather than depending on a single binary anomaly flag:

```text
amount deviation       0–40
 timing deviation      0–20
merchant unfamiliarity  0–20
category unfamiliarity 0–20
--------------------------------
composite                0–100
```

The behavioral layer is downgrade-only in the current design: a suspicious behavioral pattern can reduce an `APPROVE` to `HOLD`, but it is not used as an independent mechanism to upgrade a transaction into `BLOCK`. Hard policy violations remain the deterministic authority boundary.

---

## 6. Case-based reasoning (RAG without an external embedding service)

When a transaction is escalated, the LLM reasoner retrieves similar past cases from the **same agent's own decision history** using TF-IDF retrieval.

This was chosen because the corpus is small and structured:

- no embedding API is required,
- retrieval is deterministic,
- retrieval is cheap,
- the critical payment path does not gain another network dependency.

The LLM receives the authorized goal, current intent, transaction context, and retrieved precedent. The system is conservative: unusable or failed LLM responses fail closed to `HOLD` rather than silently becoming `APPROVE`.

---

## 7. Audit trail and data model

Sentinel stores transaction requests and decisions separately.

### `policies`

Versioned policy documents per agent.

### `transactions`

Every transaction request observed by Sentinel, including its request ID, agent, merchant, category, amount, request timestamp, stated intent, original goal, scenario tag, and processing timestamp.

### `decisions`

The judgment attached to a request, including:

- verdict,
- human-readable reasons,
- checked rules,
- decision layer,
- retrieved past-case references,
- confidence,
- model version when applicable,
- payment-gateway status/reference,
- human override for resolved `HOLD` cases,
- decision timestamp.

The database relationship is one-to-one through `request_id`.

---

## 8. API surface

Except for `/health`, the API is protected with `X-API-Key`.

### Health

```text
GET /health
```

Returns service status, database status, queue status, and replica instance ID.

### Policy

```text
POST /policies
GET  /policies
GET  /policies/{agent_id}
```

### Transactions

```text
GET  /transactions
GET  /transactions/scenarios
GET  /transactions/summary
POST /transactions/evaluate
POST /transactions/evaluate-batch
```

`GET /transactions` supports the live feed's pagination/search/filtering contract.

### Async jobs

```text
GET /jobs
GET /jobs/{job_id}
GET /jobs/batch/{batch_id}
```

### Simulation

```text
POST /simulate
POST /simulate-async
```

### Human review

```text
POST /holds/{request_id}/resolve
```

### Evaluation

```text
GET /eval/report
```

### AP2 adapter

```text
POST /ap2/evaluate-mandate
```

The adapter translates AP2-shaped mandate data into Sentinel's policy/request model. It does **not** perform production cryptographic signature verification; responses carry `signature_verified: false` so this limitation cannot be mistaken for authentication.

---

## 9. Async architecture

The synchronous path is useful for direct API calls and the seed/evaluation harness. The async path is used by the dashboard scenario runner.

```text
POST /simulate-async
       |
       +--> PurchasingAgent generates requests
       |
       +--> enqueue job payloads in Redis
                    |
             +------+------+
             |             |
          worker 1       worker 2 ...
             |             |
             +------+------+
                    |
             _process_single_evaluation
                    |
               orchestrator
                    |
             Postgres + audit
                    |
              job status update
                    |
             frontend polling
```

The current Docker deployment runs two backend replicas (`backend1`, `backend2`) behind nginx and shares Postgres and Redis.

---

## 10. Docker deployment

```text
Browser
  |
  v
nginx :8000
  |
  +--------------------+
  |                    |
backend1 :8000     backend2 :8000
  |                    |
  +---------+----------+
            |
       PostgreSQL
            |
          Redis
```

Docker Compose includes:

- `postgres` — PostgreSQL 16
- `redis` — Redis 7
- `backend1` — first API replica
- `backend2` — second API replica
- `nginx` — round-robin front door on host port `8000`

Replica identity is exposed by `/health` using `SENTINEL_INSTANCE_ID`.

---

## 11. Frontend

The frontend is a Vite/React application with Tailwind-based styling.

Main UI areas:

- **Live Feed** — transaction feed, search, verdict/scenario filters, expandable decision details.
- **Policy** — create/update agent policies.
- **Evaluation** — evaluation report, class metrics, confusion matrix, decision-layer metrics, error analysis.
- **Agent selector** — switches the dashboard between isolated agents.
- **Scenario Runner** — well-behaved/adversarial simulations, sync/async execution.
- **Queue Monitor** — async job visibility.
- **HITL controls** — resolve `HOLD` transactions.

The dashboard is not a static mock: it retrieves data from the protected API and was visually verified in the current browser-based workflow.

---

## 12. Demo scenarios

The built-in purchasing agent supports:

```text
well_behaved
amount_creep
category_drift
velocity_spike
off_hours
prompt_injection
merchant_swap
```

The demo reset script creates isolated agents so each adversarial scenario remains easy to inspect without contaminating the flagship agent's history.

### Clean flagship agent

`agent_demo_1` is intentionally only the well-behaved scenario:

```text
QuickKirana  ₹243.23  APPROVE
FreshMart    ₹623.64  APPROVE
GreenGrocer  ₹358.20  APPROVE
FreshMart    ₹788.92  HOLD  (velocity)
```

This produces the clean dashboard state:

```text
APPROVE  3
HOLD     1
BLOCK    0
TOTAL    4
```

The complete seeded database contains 18 transactions across the flagship and adversarial demo agents:

```text
APPROVE  6
HOLD     2
BLOCK   10
TOTAL   18
```

### Important recording rule

Do not repeatedly run a scenario against a seeded agent immediately before recording. The policy's velocity limit is real and history-dependent, so new attempts can legitimately become `HOLD`. Use the already-seeded adversarial agents for stable recording frames, or reset again before a new live run.

---

## 13. Evaluation

Sentinel uses multiple evaluation layers because a single 100% score can be misleading.

### Golden dataset

Current verified run:

```text
89 cases
100.0% overall accuracy
APPROVE: precision 1.00 / recall 1.00 / F1 1.00 / n=23
HOLD:    precision 1.00 / recall 1.00 / F1 1.00 / n=14
BLOCK:   precision 1.00 / recall 1.00 / F1 1.00 / n=52
rule_engine: 73/73
llm_reasoner: 16/16
misses: 0
```

In the current verified run, the live reasoning provider was OpenRouter using the configured Nemotron free model endpoint. This should be described as **100% on the 89-case golden benchmark**, not as 100% real-world accuracy.

### Prevalence-weighted synthetic set

The repository also contains a 500-case synthetic prevalence-weighted evaluation that scored 100% in its recorded report. Its class proportions are deliberately constructed rather than measured from production traffic.

### Hard/adversarial Tier 2

The current verified 16-case hard/adversarial Tier-2 run scored **93.8% (15/16)** using the live OpenRouter provider. The rule-engine portion was **11/11 (100%)** and the LLM-reasoner portion was **4/5 (80%)**. The single miss (`hard_012`) was a conservative `HOLD` for the ambiguous intent "take care of the usual order." This tier exists to expose non-trivial semantic failure modes rather than to maximize a headline score.

### Fuzz testing

187 generated adversarial variants were checked against explicit safety invariants with 0 invariant violations in the recorded report.

### Load testing

The archived load-test reports contain measured results and should be treated as historical run artifacts, not universal capacity guarantees. The observed difference between one-agent stress and many-agent spread is explained by the fact that history-dependent checks scale with the selected agent's accumulated history.

---

## 14. Testing

Current verified backend suite:

```text
113 passed
222 warnings
```

The suite covers policy rules, orchestration, HITL, agent isolation, API contracts, adversarial scenarios, Redis backends, rate limiting, behavioral risk, RAG retrieval, chaos/failure modes, and AP2 translation.

Warnings currently come from upstream/deprecation areas such as Starlette/FastAPI lifecycle APIs and SQLAlchemy's legacy UTC helper; they are cleanup items, not failing tests.

Run:

```bash
cd backend
source venv/bin/activate
python -m pytest tests/ -v
```

---

## 15. Environment variables

The important backend settings are:

```env
LLM_PROVIDER=openrouter
LLM_MODEL=<configured model>
OPENROUTER_API_KEY=<secret>

SENTINEL_DATABASE_URL=...
SENTINEL_API_KEY=...

QUEUE_BACKEND=...
CACHE_BACKEND=...
REDIS_URL=...

SENTINEL_INSTANCE_ID=...

RAZORPAY_KEY_ID=
RAZORPAY_KEY_SECRET=
```

Never commit real secrets. Keep `.env` files out of version control and use `.env.example` as a template.

---

## 16. Reliable startup

### Start Docker services

```bash
docker compose up -d --build
```

Verify:

```bash
curl -s -H "X-API-Key: dev-only-insecure-key-change-me" http://localhost:8000/health
```

The response should report:

```json
{"status":"ok", "checks":{"database":"ok","queue":"redis"}}
```

### Start frontend

```bash
cd frontend
npm install
npm run dev
```

Open:

```text
http://localhost:5173/
```

---

## 17. Safe demo reset

There may be a Postgres.app instance on the host machine using port 5432 at the same time as Docker's published PostgreSQL port. Running the seed module from the host can therefore target the wrong PostgreSQL process.

**Safest demo reset for the Docker-backed dashboard:**

```bash
docker compose exec backend1 python -m app.demo.reset_and_seed
```

This runs the seed code inside the backend container, where `postgres:5432` unambiguously identifies the Docker PostgreSQL service.

Afterward verify:

```bash
docker compose exec postgres psql -U sentinel -d sentinel -c "SELECT COUNT(*) AS transactions FROM transactions; SELECT verdict, COUNT(*) FROM decisions GROUP BY verdict ORDER BY verdict;"
```

Expected clean database state:

```text
18 transactions
APPROVE 6
BLOCK   10
HOLD     2
```

---

## 18. Security posture

Current controls include:

- API-key protection on all non-health routes.
- Sliding-window rate limiting per API key.
- Consistent JSON error envelopes.
- Durable audit writes that fail loudly if the database cannot record a decision.
- Explicit AP2 `signature_verified: false` disclosure.
- Gitignored environment files and no committed secrets.
- Conservative LLM failure behavior: errors resolve to `HOLD`, never a guessed `APPROVE`.

### Known security/production gaps

These are intentional disclosures, not hidden claims:

1. AP2 cryptographic signature verification is not implemented.
2. Rate limiting is per-instance rather than distributed across replicas.
3. The human-feedback loop is not automatically feeding overrides back into evaluation data.
4. Prompt versioning, cost/token tracking, canarying, and automated drift detection are not complete.
5. Razorpay use in this project is test-mode/mock depending on configuration; no claim of real customer charging is made.

---

## 19. Important engineering findings

The project was hardened through multiple real failures rather than only happy-path testing. High-value examples include:

- blocked attempts were incorrectly counted as spent money;
- a transaction counted itself in its own history;
- a shared word weakened prompt-injection detection;
- concurrent policy versioning could create multiple active versions with the same version number;
- RAG retrieval crashed only once enough history existed to make retrieval non-empty;
- byte-exact merchant matching let trivial case/whitespace variants bypass a blocklist;
- LLM, Redis cache, and Redis worker failure paths were not initially fail-soft;
- `scikit-learn` was missing from the declared runtime dependencies;
- golden evaluation rows leaked into the live feed;
- an amount-creep generator sometimes produced a non-violating amount;
- frontend transaction-count limits were cosmetic until server validation was added;
- multi-agent capability existed in the backend but was initially missing from the dashboard;
- requested time and processing/created time were conflated in the UI;
- the test harness could restart the singleton queue across different event loops;
- Redis queue unit tests collided with production queue data until their queue key was isolated;
- the local demo seed could target Postgres.app instead of Docker PostgreSQL when both were listening on port 5432.

These findings are part of the system's engineering evidence. They should be discussed as bugs/findings with root causes and regression coverage, not as generic "we tested it" language.

---

## 20. What Sentinel does not claim

Sentinel does **not** claim:

- 100% real-world fraud detection,
- production-ready AP2 authentication,
- real customer payment charging,
- distributed rate limiting,
- measured production traffic prevalence,
- or general LLM reasoning accuracy outside the evaluated datasets.

The strongest defensible framing is:

> **Sentinel is a tested, explainable agent-payment trust layer with deterministic policy enforcement, conservative LLM escalation, human review, auditability, adversarial scenarios, and a verified multi-replica Docker deployment — with clearly disclosed production gaps.**

---

## 21. Recommended demo narrative

The cleanest recording sequence is:

1. Start on `agent_demo_1` and show 3 approvals + 1 hold.
2. Show the policy controls.
3. Open one transaction to expose the reason and decision layer.
4. Use the agent selector to switch to a pre-seeded adversarial agent.
5. Show prompt-injection or amount-creep evidence rather than creating unnecessary new history.
6. Demonstrate one `HOLD` resolution if useful.
7. Show Evaluation: **89 cases / 100.0%** and read the value live.
8. Show the current test result: **113 passed**.
9. Disclose AP2 signature verification and per-instance rate limiting before the close.

This sequence is stable, visually rich, and avoids accidental demo-state corruption.

---

## 22. Repository map

```text
backend/
  app/
    agents/       LangGraph purchasing-agent simulator
    api/          FastAPI routes and request processing
    core/         policy evaluation, risk, LLM, queues, cache, auth, AP2
    demo/         deterministic reset/seed runner
    eval/         golden, prevalence, and hard evaluation assets
    loadtest/     measured async throughput tests
    fuzz/         adversarial property/invariant tests
    models/       SQLAlchemy + policy/domain models
    rag/          TF-IDF case retrieval
  tests/          backend regression and contract tests
  data/           evaluation and load/fuzz reports
frontend/
  src/
    components/   dashboard UI components
    api.js        authenticated API client
README.md
  project overview, runbook, evaluation, limitations

docs/ARCHITECTURE.md
  detailed system architecture and engineering evidence
```

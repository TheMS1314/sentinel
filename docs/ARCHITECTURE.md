# Sentinel — Detailed Architecture and Engineering Design

> **Purpose:** This document is the engineering-level description of Sentinel: data flow, components, decision semantics, persistence, asynchronous execution, frontend contracts, evaluation methodology, deployment, failure behavior, security posture, known limitations, and the bugs/findings that shaped the final design.

---

## 1. System purpose

Sentinel is an authorization-and-trust layer for AI agents that can initiate payments.

The problem is not simply "can a payment be fraudulent?" The product boundary is narrower and more specific:

> **Is the transaction the software is attempting to make still within the financial and behavioral authority the human gave that agent?**

The system therefore treats the policy as the source of authority and the transaction request as untrusted input that must be evaluated before money movement.

The trust boundary is:

```text
Human-owned policy
       |
       v
+-------------------------+
|        SENTINEL         |
|                         |
| authenticate request    |
| fetch policy/history    |
| audit request           |
| deterministic checks    |
| optional risk scoring   |
| optional LLM reasoning  |
| audit decision          |
| HITL when required      |
+-------------------------+
       |
       +---- APPROVE ----> payment gateway
       |
       +---- HOLD -------> human review
       |
       +---- BLOCK ------> stop
```

The gateway shown in the project is Razorpay **test-mode/mock**, not a production card-charging integration.

---

## 2. High-level runtime topology

The Docker Compose deployment uses multiple backend replicas behind nginx.

```text
                              Browser
                                 |
                                 | http://localhost:5173
                                 v
                           React / Vite UI
                                 |
                                 | authenticated REST API
                                 v
                        +-------------------+
                        | nginx :8000       |
                        | round-robin proxy |
                        +---------+---------+
                                  |
                 +----------------+----------------+
                 |                                 |
                 v                                 v
        +----------------+                +----------------+
        | backend1       |                | backend2       |
        | instance_id=1  |                | instance_id=2  |
        +--------+-------+                +--------+-------+
                 |                                 |
                 +---------------+-----------------+
                                 |
                    +------------+------------+
                    |                         |
                    v                         v
             +-------------+          +-------------+
             | PostgreSQL  |          |    Redis    |
             | audit/data  |          | queue/cache |
             +-------------+          +-------------+
```

Both backend replicas use the same Postgres and Redis. Replica identity is intentionally visible through `/health` so the load-balancer topology can be proven rather than only documented.

---

## 3. Request lifecycle

A request follows these semantic stages.

### 3.1 Authenticate

All routes except `/health` require a valid `X-API-Key`.

This is an access-control boundary, not merely a UI convenience.

### 3.2 Load policy

The evaluator retrieves the active policy for `request.agent_id`.

Policies are versioned. Updates deactivate prior versions and increment the active version so "latest policy" is a real database property rather than an application guess.

### 3.3 Fetch history before logging the current request

This ordering is deliberate:

```text
history = prior requests only
              |
              v
log current request
              |
              v
evaluate current request
```

Logging first caused the current request to appear in its own history and made velocity checks effectively one request too aggressive.

### 3.4 Audit-log the request

Every request is recorded even when it is later blocked or held.

The audit trail distinguishes:

- the requested event,
- the judgment about that event,
- and later payment/HITL outcomes.

### 3.5 Deterministic rule engine

The rule engine evaluates explicit policy controls.

Checks include:

```text
per-transaction amount
 daily spend
allowed category
blocked merchant
optional merchant allowlist
time window
velocity
cheap intent-drift / override detection
```

Decision semantics:

```text
Hard violation            -> BLOCK
Velocity-only violation   -> HOLD
Intent mismatch needing   -> ESCALATE
reasoned judgment
Everything clean          -> APPROVE
```

### 3.6 Behavioral risk scoring

The composite risk layer can add a downgrade signal based on behavioral novelty:

```text
amount deviation       0–40
 timing deviation      0–20
merchant unfamiliarity  0–20
category unfamiliarity 0–20
                         ----
                         0–100
```

The intended semantics are conservative. Behavioral anomaly detection does not replace explicit policy enforcement and does not turn a clean request into a hard `BLOCK` by itself.

### 3.7 Escalation to LLM reasoner

Only requests that are not already resolved by a hard deterministic rule and require semantic judgment are escalated.

The reasoner receives:

- authorized human goal,
- stated agent intent,
- merchant/category/amount context,
- relevant policy context,
- retrieved precedent from the same agent.

The current prompt explicitly treats contradictory/override language as a boundary violation and prefers `HOLD` when intent is plausible but not clearly authorized.

### 3.8 Retrieve past cases

`CaseRetriever` uses TF-IDF over prior decisions from the same agent.

The retrieval design is intentionally local to each agent because the agent's own history is the most relevant precedent for its authorization context.

The design avoids placing an external embedding service in the payment-critical path.

### 3.9 Parse and validate LLM output

The LLM client requests structured JSON when supported by the provider configuration.

The system validates:

- a usable response exists,
- JSON can be interpreted,
- a verdict is present and valid.

The current live client also retries transient/unusable responses once. After the retry budget is exhausted, the reasoner fails closed to `HOLD`.

### 3.10 Audit-log the decision

The final `DecisionRecord` contains the verdict, reasons, checked rules, decision layer, similarity references, confidence/model metadata where available, and payment/HITL state.

### 3.11 Payment gateway boundary

Only `APPROVE` proceeds toward the Razorpay gateway abstraction.

`HOLD` waits for a human resolution.

`BLOCK` never reaches the gateway.

---

## 4. Domain models

### 4.1 `AgentPolicy`

A policy is pure data, represented as a JSON-serializable domain object.

Important fields:

```text
policy_id
agent_id
owner_human_id
max_amount_per_txn
max_daily_total
allowed_categories
blocked_merchants
allowed_merchants
time_window
velocity
active
version
```

### 4.2 `TransactionRequest`

Represents an incoming spend attempt.

```text
request_id
agent_id
merchant_name
merchant_category
amount
requested_at_hour
requested_at_iso
stated_intent
raw_agent_goal
```

### 4.3 `Decision`

Represents Sentinel's judgment.

```text
request_id
verdict
reasons
checked_rules
decided_by
similar_past_cases
confidence
model_version
```

### 4.4 `HistoryItem`

Pairs a past transaction request with its outcome.

It also implements the important `counts_as_spent` distinction:

```text
APPROVE                          -> counts as spent
HOLD resolved to APPROVE        -> counts as spent
BLOCK                           -> does not count as spent
HOLD still pending              -> does not count as spent
```

This distinction fixed the earlier bug in which blocked attempts poisoned the daily spend total.

---

## 5. PostgreSQL schema

The database contains three core tables.

### `policies`

Stores versioned policy JSON and version/active metadata.

### `transactions`

Stores request facts:

```text
id
request_id
agent_id
merchant_name
merchant_category
amount
requested_at_hour
requested_at_iso
stated_intent
raw_agent_goal
scenario_tag
created_at
```

### `decisions`

Stores judgments:

```text
id
request_id -> transactions.request_id
verdict
reasons
checked_rules
decided_by
similar_past_cases
confidence
model_version
razorpay_status
razorpay_ref
human_override
created_at
```

`request_id` is the one-to-one relational link between the transaction fact and the decision judgment.

Separating the tables matters because the audit record of "what the agent asked for" remains distinct from the later judgment about it.

---

## 6. Daily-spend and velocity semantics

These two controls intentionally answer different questions.

### Daily total

Measures money that actually moved or was later approved by a human.

A blocked ₹5,000 request does not mean ₹5,000 was spent.

### Velocity

Measures attempt frequency. A burst of blocked/held requests can itself be suspicious, so velocity intentionally counts attempts rather than only approved spend.

This distinction is central to the policy semantics and was covered by regression tests.

---

## 7. Policy versioning and multi-replica correctness

The system initially allowed multiple active policy rows for the same agent at the same version number.

That created a data-model ambiguity that looked like a distributed cache problem because one replica could appear to observe a different policy version than another.

The final design makes versioning explicit:

```text
existing active policy
        |
        +--> deactivate
        |
        +--> version + 1
        |
        +--> insert new active record
```

This means the latest policy is determined by durable state, not by which process happened to update it most recently in memory.

---

## 8. Deterministic purchasing-agent simulator

`PurchasingAgent` is implemented with LangGraph as a small state machine.

Two main paths exist:

```text
well_behaved
     |
     +--> clean grocery requests

adversarial
     |
     +--> amount_creep
     +--> category_drift
     +--> velocity_spike
     +--> off_hours
     +--> prompt_injection
     +--> merchant_swap
```

The normal demo path is deterministic enough to produce a stable recording sequence while adversarial paths deliberately violate selected controls.

A particularly important correction was the amount-creep generator: an attack must actually exceed the policy instead of occasionally generating a legal amount.

---

## 9. Category semantics

The policy's normal category is:

```text
grocery
```

The project explicitly avoids the earlier inconsistent `grocery_stores` label.

Normal grocery requests use `merchant_category = "grocery"`.

Category drift must remain a genuinely different category so that the attack demonstrates a real policy boundary instead of silently normalizing the attack away.

---

## 10. Intent-drift and prompt-injection model

Intent drift is deliberately treated as its own trust concern.

A naive keyword-overlap heuristic had a concrete flaw: an injected instruction such as an "ignore previous instructions" phrase could still share the common word "buy" with an authorized goal and look superficially aligned.

The corrected design:

1. removes common stopwords from simple overlap reasoning;
2. detects explicit override/bypass language;
3. routes uncertain intent to the LLM reasoner instead of auto-approving.

The live reasoner prompt is also conservative about purpose boundaries. A broad goal such as weekly groceries does not automatically authorize every vaguely related purpose such as gifts, baking, a dinner party, or another unmentioned event.

---

## 11. LLM reasoner architecture

The LLM layer is a secondary judgment mechanism.

```text
Rule engine
    |
    | ESCALATE only
    v
+-------------------------+
| Case retrieval          |
| TF-IDF, agent-local     |
+------------+------------+
             |
             v
+-------------------------+
| LLMReasoner             |
|                         |
| policy + goal + intent  |
| + current transaction   |
| + similar past cases    |
+------------+------------+
             |
             v
       strict JSON verdict
             |
       +-----+-----+
       |     |     |
    APPROVE HOLD BLOCK
```

The production-oriented fail-soft boundary is intentionally asymmetric:

```text
LLM unavailable -> HOLD
LLM invalid      -> HOLD
LLM timeout      -> HOLD
```

The system does not treat LLM unavailability as evidence that a payment is safe.

---

## 12. Live model and evaluation semantics

The current configured runtime uses OpenRouter.

A verified current golden-dataset run produced:

```text
89 / 89 correct
100.0% overall
rule_engine 73 / 73
llm_reasoner 16 / 16
```

This is a valid benchmark result, but it is not a claim of universal reasoning quality. The benchmark is a curated 89-case dataset designed for coverage.

The repository also contains:

- a 500-case synthetic prevalence-weighted evaluation with a recorded 100% result;
- a 16-case hard/adversarial tier with a current verified live-provider result of 93.8% (15/16);
- a 187-variant fuzz run with 0 invariant violations in the recorded report.

The hard tier is especially important because it was introduced after easier sets saturated at 100%.

---

## 13. Evaluation-data isolation

The evaluation harness persists scoring transactions because some cases require realistic history state.

That creates a risk if those rows appear in the user-facing live feed.

The final design therefore distinguishes persisted evaluation history from live dashboard views, including filtering `golden_*` scenario-tagged records out of live-facing endpoints while retaining them for evaluation correctness.

The engineering lesson is important:

> Evaluation data may need to exist in the same storage system as production-like history, but it must not silently become production-like UI data.

---

## 14. Async queue design

### Submission

`POST /simulate-async`:

1. validates the active policy;
2. invokes `PurchasingAgent` to generate requests;
3. assigns a `batch_id`;
4. enqueues each request as a job;
5. returns job IDs plus the batch ID.

### Worker

Workers pop jobs from Redis and execute the same core evaluation path used by synchronous processing.

### Job state

The queue records states sufficient for the dashboard to distinguish queued, running, completed, and failed jobs.

### Batch polling

The frontend polls the single batch endpoint rather than making one independent request for every job. This reduces request fan-out and gives the UI one coherent completion view.

---

## 15. Redis queue test isolation

A subtle development issue occurred because local Redis queue tests and the production-style Docker queue used the same hard-coded Redis list key.

The production workers then attempted to process test payloads that did not contain the fields production evaluation required.

The fix was architectural rather than a production-workaround:

- production keeps its normal queue key;
- unit tests can inject a queue key;
- test queues use a dedicated namespace.

The corrected Redis queue tests passed, and the full backend suite subsequently reached 113 passing tests.

---

## 16. Cache semantics and fail-soft boundaries

Caching is an optimization, not a correctness dependency.

Redis cache operations therefore fail soft:

```text
cache unavailable
      |
      +--> warning/log
      +--> treat as miss/no-op
      +--> continue request
```

The queue is different because it is part of asynchronous work delivery. Worker connection errors are caught and retried with backoff rather than silently terminating the task.

The audit database is deliberately **not** fail-soft. If Sentinel cannot durably record the request/decision, continuing as though the evaluation succeeded would contradict the product's trust model.

---

## 17. API architecture

The FastAPI application exposes:

```text
POST /policies
GET  /policies
GET  /policies/{agent_id}

GET  /transactions
GET  /transactions/scenarios
GET  /transactions/summary
POST /transactions/evaluate
POST /transactions/evaluate-batch

POST /simulate
POST /simulate-async

GET  /jobs
GET  /jobs/{job_id}
GET  /jobs/batch/{batch_id}

POST /holds/{request_id}/resolve

GET  /eval/report
POST /ap2/evaluate-mandate

GET  /health
```

Error handling is normalized into a consistent JSON envelope instead of leaking raw framework tracebacks to API consumers.

---

## 18. Frontend architecture

The frontend is split into focused components rather than putting all behavior into `App.jsx`.

Key components include:

```text
AgentSelector
LiveFeed
StatsBar
PolicyEditor
ScenarioRunner
QueueMonitor
EvalResults
Toast
ErrorBoundary
ThemeToggle
Badges
```

`src/api.js` centralizes:

- base URL configuration,
- API key handling,
- JSON request serialization,
- 15-second request timeout,
- API error mapping.

This prevents every component from independently inventing networking behavior.

---

## 19. Multi-agent isolation

The agent selector is not only a UI filter.

The selected `agent_id` drives:

- policy retrieval,
- transaction feed queries,
- summary queries,
- scenario execution context.

Each agent therefore has its own policy and transaction history.

The seeded demo deliberately uses different agent IDs for adversarial scenarios so the clean flagship agent remains clean.

---

## 20. Timestamps and event semantics

Sentinel has two important time concepts:

```text
requested_at_*  = when the agent says the transaction is happening
created_at      = when Sentinel processes/records it
```

The demo generator uses a deterministic reference time so that evaluation cases are reproducible.

The UI was updated to distinguish requested/event time from created/processed time rather than showing one timestamp as though it meant both.

This matters for demo realism: deterministic evaluation data can otherwise make a recording appear to contain stale requests from a week ago even when Sentinel processed them moments earlier.

---

## 21. AP2 compatibility

Sentinel includes an AP2-shaped mandate translation adapter.

Conceptual mapping:

```text
AP2 Intent Mandate  -> AgentPolicy
AP2 Cart Mandate    -> TransactionRequest
Sentinel verdict    -> authorization decision
```

The concrete adapter is exposed at:

```text
POST /ap2/evaluate-mandate
```

### Explicit limitation

The adapter translates mandate shape; it does not implement the real cryptographic signature verification required in a production AP2 trust boundary.

Every response carries:

```text
signature_verified: false
```

This is intentional. A partially implemented security property is disclosed instead of being represented as complete authentication.

---

## 22. Security controls

Current controls include:

1. `X-API-Key` authentication on protected routes.
2. Sliding-window rate limiting.
3. Structured request/response error envelopes.
4. Durable audit records.
5. Gitignored environment secrets.
6. Conservative LLM failure behavior.
7. Explicit AP2 signature-verification disclosure.

### Rate limiting scope

The current limiter is per-instance.

With two replicas:

```text
client
  |
  +--> backend1 limiter
  |
  +--> backend2 limiter
```

The counters are not a single distributed quota. This is documented as a production-scaling gap, not hidden behind the load balancer.

---

## 23. Razorpay boundary

`RazorpayGateway` is an abstraction around the payment provider.

The project supports:

```text
MOCK MODE
   |
   +--> complete end-to-end demo without credentials

TEST MODE / real SDK integration
   |
   +--> available when Razorpay test credentials are supplied
```

The demo should always say **payment gateway** or **Razorpay test mode** rather than implying a real customer card was charged.

---

## 24. Evaluation methodology in depth

### 24.1 Golden set

The 89-case set uses coverage-driven construction / equivalence partitioning.

It deliberately includes:

- each policy rule,
- boundary values,
- over-limit values,
- adversarial cases,
- ambiguous intent cases.

Current verified result:

```text
89 cases
100.0% overall
```

### 24.2 Synthetic prevalence-weighted set

500 synthetic cases approximate a realistic transaction mixture. The class proportions are estimated for testing purposes rather than measured from real production traffic.

### 24.3 Hard/adversarial tier

16 cases target:

- merchant near misses,
- semantic intent judgment without simple keyword cues,
- compound weak signals,
- behavioral-baseline limitations.

The recorded result is:

```text
93.8% overall (15/16 correct)
Rule engine: 11/11 (100%)
LLM reasoner: 4/5 (80%)
Live provider: OpenRouter
Single miss: hard_012 -> conservative HOLD
```

This tier exists precisely to prevent a false sense of security from benchmark saturation.

### 24.4 Fuzzing

187 programmatically generated cases probe:

```text
injection phrasing
amount boundaries
category swaps
combined attacks
```

The recorded invariant run produced:

```text
0 invariant violations
```

### 24.5 Load tests

Load tests use the real async path and measure completion latency/throughput rather than estimating them.

The central finding is that performance depends strongly on the number of transactions concentrated on one agent because history-dependent checks and case-index rebuild work scale with that agent's history.

Historical report values should be read as measured runs for the exact test configuration, not as a universal SLA.

---

## 25. Bug and finding history

The engineering story is valuable because the final architecture exists partly because concrete tests exposed concrete failures.

### 25.1 Blocked spend counted as actual spend

**Cause:** daily-total used all request history.

**Fix:** only approved money movement counts toward spend; velocity still counts every attempt.

**Regression:** added.

### 25.2 Self-counting history

**Cause:** current request logged before history fetch.

**Fix:** history first, log second.

### 25.3 Prompt-injection keyword bypass

**Cause:** shared words could make malicious intent look aligned.

**Fix:** stopword filtering + explicit override-language detection + escalation.

### 25.4 Concurrent policy version ambiguity

**Cause:** prior policy wasn't deactivated/versioned correctly.

**Fix:** deactivate old active version and increment version.

### 25.5 RAG empty/non-empty path gap

**Cause:** missing dataclass field was only touched after multiple escalations created retrieval matches.

**Fix:** model field + direct formatter test + multi-escalation regression.

### 25.6 Merchant blocklist normalization

**Cause:** byte-exact merchant matching.

**Fix:** case-fold + strip both sides.

A genuinely different merchant such as `ShadyStore2` is intentionally not fuzzy-matched to avoid false positives.

### 25.7 Fail-soft dependency gaps

Found via chaos testing:

- LLM outage could crash evaluation;
- mid-session Redis failure could crash cache access;
- transient Redis disconnect could silently kill a queue worker.

The fixes were explicit and regression-tested.

### 25.8 Missing declared dependency

`scikit-learn` was used for TF-IDF retrieval but missing from the declared dependency set. A fresh environment caught the issue.

### 25.9 Incorrect prior completion assumptions

Several fixes were claimed in an earlier session but did not exist in the actual files when directly verified. The process change was to verify every completion claim against code/tests instead of trusting a narrative summary.

### 25.10 Evaluation rows leaked into live feed

The eval harness persisted rows the live feed could also see.

**Fix:** retain data for evaluation but exclude `golden_*` rows from live-facing views.

### 25.11 Non-deterministic/non-attacking amount creep

The adversarial generator could produce amounts below the policy cap.

**Fix:** constrain the generator so the scenario truly attacks the intended control.

### 25.12 Client-only input validation

HTML `min`/`max` did not create a real boundary for arbitrary JavaScript requests.

**Fix:** server-side Pydantic bounds plus frontend clamping as a courtesy.

### 25.13 Multi-agent capability invisible in UI

The backend supported agent isolation but the frontend did not initially expose it coherently.

**Fix:** real agent selector and agent-scoped dashboard queries.

### 25.14 Requested vs processed timestamp confusion

**Fix:** expose processing/created time separately from event/request time.

### 25.15 Test harness lifecycle mismatch

Module-level async queue state was restarted across different test event loops.

**Fix:** test-client lifecycle aligned with the real application lifecycle.

### 25.16 Redis test/production queue collision

Local Redis unit tests shared the production queue key.

**Fix:** injected queue key + dedicated test key namespace.

### 25.17 Environment-targeting finding: Postgres.app vs Docker Postgres

Two PostgreSQL servers were listening on host port 5432: Postgres.app and Docker's published PostgreSQL port. Running the seed script locally therefore could reset the wrong server.

This is an operational environment hazard rather than a Sentinel policy bug.

**Safe practice:** run the demo reset inside `backend1` so the database host is unambiguous (`postgres:5432`).

---

## 26. Test status

Current verified backend state:

```text
113 passed
222 warnings
```

The warnings are non-blocking upstream/deprecation warnings. They do not change the current correctness result.

The important point is that the suite was executed from the project's virtual environment using:

```bash
python -m pytest tests/ -v
```

rather than relying on a potentially unrelated global `pytest` executable.

---

## 27. Current demo state

The clean Docker database after reset contains:

```text
18 transactions
6 APPROVE
2 HOLD
10 BLOCK
7 policies
```

The flagship agent `agent_demo_1` intentionally contains only:

```text
3 APPROVE
1 HOLD
0 BLOCK
4 total
```

The adversarial records use separate demo agents.

This separation is deliberate because it lets the recording begin with a clean hero agent and then switch to stable, pre-seeded attack examples.

---

## 28. Operational runbook

### Start everything

```bash
docker compose up -d --build
cd frontend
npm install
npm run dev
```

### Check backend health

```bash
curl -s http://localhost:8000/health
```

### Safe demo reset

```bash
docker compose exec backend1 python -m app.demo.reset_and_seed
```

### Verify database contents

```bash
docker compose exec postgres psql -U sentinel -d sentinel -c "SELECT COUNT(*) FROM transactions; SELECT verdict, COUNT(*) FROM decisions GROUP BY verdict ORDER BY verdict;"
```

### Run backend tests

```bash
cd backend
source venv/bin/activate
python -m pytest tests/ -v
```

### Run golden evaluation

```bash
python -m app.eval.eval_harness
```

### Inspect API summary

```bash
curl -s -H "X-API-Key: dev-only-insecure-key-change-me" \
  http://localhost:8000/transactions/summary
```

---

## 29. Production-readiness roadmap

The following are the next architectural steps, not current claims:

### Security

- implement real AP2 signature verification;
- rotate secrets through a real secret manager;
- make rate limiting distributed.

### LLM operations

- prompt versioning independent of code;
- token/cost tracking;
- model canary/shadow evaluation;
- automated decision-distribution drift detection;
- model-quality dashboards separated from deterministic policy metrics.

### Human feedback

Use the existing `human_override` field to build a feedback loop that regularly turns disagreements into new labeled evaluation cases.

### Data and scale

- replace the Redis list with a purpose-built durable broker where justified;
- partition/shard large audit histories by agent/time;
- consider materialized summary tables for very large transaction volumes;
- move expensive case-index rebuild work toward incremental/background maintenance.

---

## 30. Architectural principles that should not be weakened

1. **Policy is human-owned authority.** The agent does not self-modify its permissions.
2. **Deterministic rules stay deterministic.** Do not replace obvious policy comparisons with an LLM.
3. **Uncertainty fails closed.** LLM failure must never become an implicit approval.
4. **Auditability is a correctness property.** Do not silently lose decision records.
5. **Evaluation data must not masquerade as live traffic.**
6. **Adversarial demos must remain real attacks.** Do not weaken the policy just to create visually balanced results.
7. **Production gaps should be disclosed explicitly.**
8. **Fix causes at the correct layer.** Do not hide backend correctness problems with frontend transformations.

---

## 31. Final architecture summary

Sentinel is best understood as four connected systems:

```text
1. AUTHORIZATION
   Human-owned policies and deterministic rules

2. REASONING
   Behavioral signals + TF-IDF precedent + conservative LLM escalation

3. ACCOUNTABILITY
   Durable transaction/decision audit trail + HITL resolution

4. DELIVERY
   Sync + Redis-backed async processing + multi-replica API + React dashboard
```

That combination is the actual product boundary. The UI demonstrates it, the evaluation harness measures it, the adversarial scenarios attack it, and the audit trail explains it.

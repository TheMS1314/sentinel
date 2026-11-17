# Sentinel architecture

## Runtime

The application is a modular Python backend, not a collection of microservices. nginx routes API traffic to two replicas and serves the separately built React frontend. PostgreSQL holds policy/history/decision/payment/session records. PostgreSQL also owns accepted background jobs; Redis holds shared quota counters. Internal evaluations call the same orchestrator as the scoped API.

## Authority

The credential registry defines subject, role and owner/agent scope. Browser credentials are exchanged for revocable HttpOnly sessions. Cookie-based writes require explicit origin and CSRF headers. Administrative methods and agent methods have separate permissions; feeds and policies are scoped on the server. Queued work rechecks that its submitting subject still exists and has access.

An owner-configured goal is part of the policy. The server supplies processing time and goal for public submissions. The normal API cannot submit scenario tags or internal fields. Simulation is explicitly a non-production operation. Merchant/category assertions are validated in shape, not independently authenticated; live payment keys are therefore unsupported.

## Transaction and state boundaries

1. Validate fields and exact money representation.
2. Serialize agent mutations with a PostgreSQL transaction advisory lock. SQLite uses a local development lock.
3. Reload current policy under that lock and recognize stable request replays. A reused ID with changed request facts conflicts.
4. Compute full-ledger allowance totals and attempt counts using SQL. Only the statistical history is bounded to 200 approved-order observations; totals are never truncated to that limit.
5. Apply rules. For semantic ambiguity, prepare bounded retrieval, release the transaction for model IO, then reacquire and recheck current policy, clock, allowance and job lease. A changed policy invalidates earlier reasoning. Persist the request and downgrade candidate approval for risk.
6. Commit request, decision, policy snapshot, and any pending payment reservation atomically.
7. Atomically claim the pending payment entry, commit the claim, then attempt external IO.
8. Persist provider status. Unknown outcomes stay reserved and are never automatically repeated.

Created orders and mock results reserve allowance but are not counted as captured money. A verified capture webhook or verified order reconciliation changes settlement state. An external call cannot share a transaction with PostgreSQL; a durable outbox plus explicit reconciliation handles that boundary conservatively.

Human review accepts only a pending HOLD. Under the same agent lock it rechecks current hard policy and allowance before creating one reservation. The original verdict remains unchanged; a separate review event records actor, reason, and verdict. Invalid/repeated transitions conflict.

## Rules, semantics and risk

Rules cover active policy, individual/daily limits, normalized merchant lists, category, UTC hours and rolling attempt velocity. Exact money comparisons use integer paise; persisted values use numeric columns. Velocity counts attempts; daily allowance counts reservations/captures. Explicit override phrases block deterministically. Only an exact canonical goal restatement avoids semantic review; one shared word cannot grant authority.

The reasoner receives policy context and untrusted request data, plus up to three TF-IDF matches. Retrieval is bounded to 500 cases from the same agent, policy and goal, preceding the request. Human corrections replace original precedent. The index is rebuilt rather than relying on stale cross-replica cache invalidation. Retrieval is optional for ablation; no measured causal benefit is asserted.

Live responses are locally validated for verdict, reasoning and finite confidence within 0..1. Timeout/retry budgets are bounded; unusable responses hold. Mock semantics hold ambiguous requests. Prompt version/hash, model/mode, usage, and any reported cost are saved; unknown costs remain unknown.

Risk uses approved-order history, excluding unresolved unknown executions. Amount z-score, circular hour distance and normalized merchant/category familiarity produce a 0..100 heuristic with a 30-point review threshold. It can only downgrade approval to HOLD. Model self-confidence and risk are separate fields in persistence and UI. Weights are not calibrated probabilities.

## Local risk intelligence and emergency dispatch guard

Non-blocked decisions receive an immutable original intelligence snapshot: owner-scoped merchant/domain posture evidence, exact-domain threat matches, and bounded same-owner relation signals. Sources are explicitly operator/synthetic; commercial providers are disconnected. Stale evidence and uncalibrated exposure/coordination signals cause review; confirmed operator indicators with an operator mapping block. Unknown coverage never grants authority.

Payment dispatch obtains a separate global advisory transaction lock, rechecks versioned emergency controls and current confirmed threats, then commits a unique dispatch claim with the processing transition before external IO. Count/amount windows include failed and unknown claims. A freeze stops new claims, while previous claims may complete. Dispatch also checks the bound policy, current actor scope and a five-minute default authorization deadline. Paused payments stay reserved until dispatch, cancellation or expiry; unknown effects stay reserved for reconciliation. Stale processing becomes unknown, and a late response cannot downgrade capture. The outbox retries fairly with bounded parallelism. Review actions cannot override a current confirmed threat. This is a separate control inside the same application/database, not a fault-isolated network service.

Administrator evidence/guard updates share the dispatch lock and record actor plus before/after state. See [risk intelligence design](RISK_INTELLIGENCE.md) for default behavior, matching limits, source freshness, APIs, and future licensed connectors.

## Queue, quotas and failure recovery

PostgreSQL atomically admits bounded batches and stores jobs, payloads, subjects, status and results. Workers claim independent rows with SKIP LOCKED and renewable opaque leases. The current token is checked under a job row lock before financial authorization and when completing work. Dependency failures reuse the stable request ID; exhausted retries become visible failed jobs. Never-started work expires after five minutes by default. Retention preserves incomplete batches and does not delete the financial ledger.

The production profile rejects in-memory and legacy Redis queues. Startup refuses undrained hardened-v2 Redis work rather than silently switching authority. Development compatibility queues retain their documented limits. Accepted SQL work survives loss of Redis; new HTTP admission fails closed if its quota service is unavailable.

Distributed quota checks use Redis server time and an atomic sliding-window script per authenticated subject. Quota-backend outages fail closed. Cache helper failures can degrade to misses, but cached policies are not an authorization source. The current authorization path reads PostgreSQL directly.

## UI and operations

The dashboard supports scoped agents, policies, filtered/paginated feed, explicit requested/processed times, queued scenarios, review and evaluation reports. Request generation counters reject outdated feed/policy responses. Periodic refresh uses current context, batch polls do not overlap, pending review buttons are disabled, and timers are cleaned up. Browser assets contain no service credential.

Compose uses non-root containers, a read-only backend filesystem, unexposed database/cache services, migrations and loopback publication. An external TLS ingress is required before HTTPS browser deployment. Production-profile configuration rejects development credentials, private queues, missing reasoning/test-provider keys, and non-HTTPS allowed origins.

`/health/live` checks the process; `/health/ready` and `/health` check database/queue access, quota availability, worker tasks and outbox progress. Runtime admission, pools and timeouts are bounded; forwarded headers require explicit proxy trust. `/metrics` exposes payment states, review backlog, reported model usage/cost, and a descriptive decision-distribution change signal to admins. Distribution alerts never modify policy or approve payments. `/feedback/cases` exports scoped human labels requiring review; it never trains or changes models automatically.

## Evidence

Tests cover authority, money precision, concurrency, replay, human review, failure behavior, migration, Redis recovery, API contracts, frontend sessions and stale responses. Evaluations and benchmarks use isolated databases and explicit runtime metadata. Hard-set timestamp and path-isolation defects were corrected. Mock-mode benchmark errors remain visible instead of being relabeled to improve scores.

See HARDENING_REPORT.md for executed checks and remaining external verification. This architecture does not claim universal injection resistance, verified merchant identity, live banking deployment, or exactly-once effects across a provider and database.

Operational limits, incident handling, upgrade steps and deployment boundaries: [production engineering](PRODUCTION_READINESS.md).

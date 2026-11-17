# Sentinel hardening review

This branch replaces several prototype assumptions with explicit authority, accounting and recovery boundaries. The changes are published on `codex/sentinel-hardening` in [pull request #1](https://github.com/TheMS1314/sentinel/pull/1). Main remains unmerged.

## Current follow-up work

[The remaining gap register](GAP_REGISTER.md) is authoritative for the current implementation. Beyond the original runtime hardening, the branch now includes shared credential lifecycle management, versioned operator merchant identities, incident workflows and signed HTTPS notifications, Recorded Future/RiskRecon adapter contracts, restricted database grants, TLS/replica deployment rendering, explicit integration probes and independently curated chronological evaluation. The final review also closes reconciliation credential rotation races and removes the need to retain old bootstrap material after initialization.

Latest [run 44091900923](https://github.com/TheMS1314/sentinel/actions/runs/44091900923), implementation `d5601fbc3ad36b2702e5acf5cfff50a22d97ebc6`, passed **269 backend tests, 19 frontend tests and all 39 container checks**, runtime audits, real Kubernetes API schema dry run and actual nginx configuration validation. The follow-up adds opt-in scheduled commercial enrichment with replica leases and fenced responses, reviewed identity-version bindings, source-clock/watermark checks, monitoring scan fairness and atomic private report writes. Real containers proved missing credentials become durable unavailable evidence and HOLD, with one evidence append across replica workers. No licensed provider call is claimed. [Current evidence](verification/provider-refresh-ci.json); [earlier gap-closure evidence](verification/gap-closure-ci.json). Historical counts below describe their cited snapshots.

## Gap-by-gap outcome

| Area found during review | Change |
|---|---|
| Shared administrator/agent authority | Role/owner/agent-scoped credentials, server-side query filters, reviewer permissions and queued-subject recheck |
| Browser service key embedded in assets | Sign-in exchange, hashed/revocable HttpOnly sessions, origin and CSRF checks; no frontend API key |
| Caller-defined goal/clock | Owner goal stored in policy; public requests use current server/DB processing time after acquiring the agent lock |
| Unvalidated direct and batch input | Bounded Pydantic schemas, strict extra fields, valid identifiers/timestamps, positive finite two-decimal money, bounded batches |
| Racy policy updates | Per-agent PostgreSQL advisory transaction lock, unique agent/version and one-active-policy database constraints |
| Missing decision provenance | Policy identity/version/snapshot, prompt version/hash, model and runtime mode, reported usage/cost; unknown cost stays unknown |
| Approximate floating-point accounting | Numeric persisted amounts and integer-paise comparisons/reservations |
| Allowance limited to last 200 rows | Full-ledger SQL totals/counts; only approved behavioral observations are bounded |
| Concurrent overspending | Shared per-agent authorization lock and atomic approval/reservation commit |
| Approval confused with payment | Explicit pending/processing/created/mock/unknown/captured states; only capture counts as completed spending |
| Replay/duplicate external orders | Request facts bound to stable IDs, atomic conditional outbox claim, no automatic repetition after unknown outcome |
| Crash between decision and provider call | Durable pending payment outbox, background recovery, admin reconciliation and signed deduplicated capture webhook |
| Unguarded hold resolution | Existing pending HOLD required, single transition, latest hard-policy/allowance recheck, reviewer/reason/time and separate review event |
| One shared keyword approved unrelated intent | Exact canonical goal restatement fast path; added purposes require reasoning; explicit override phrases block deterministically |
| Mock output looked like model approval | Conservative ambiguous HOLD and explicit mode metadata |
| Invalid LLM output/confidence | Local schema validation, bounded timeout/retry, finite 0..1 confidence, safe HOLD on unusable output |
| Unscoped/stale retrieval | Same-agent/policy/goal/prior-time corpus, human-corrected precedent, bounded index rebuild, ablation switch |
| Risk confidence mixed with model confidence | Separate risk field and UI label; circular hour distance, normalized merchants, corrected zero-variance explanation |
| Accepted work could be lost or redelivered across failures | Authoritative PostgreSQL queue with atomic batch admission, renewable leases and token fencing of authorization and completion |
| Dependency outage changed queue authority | SQL jobs remain durable; Redis quota failures close new HTTP admission; no fallback |
| Per-replica quota | Redis-time atomic sliding-window quota per subject; retry header preserved; quota outage fails closed |
| Dashboard refreshed wrong context | Request-generation/context checks, current polling closures, non-overlapping batch polls, timer cleanup and pending review buttons |
| Policy form lost strict merchant lists | Allowlist retained/editable; owner identity and authoritative goal represented explicitly |
| Evaluation UI implied live mode from provider | Explicit runtime mode, saved current results, removed hardcoded progression claims |
| Evaluation contaminated application state | Dedicated disposable databases; endpoint filtering covers older internal tags; evaluation cannot invoke a real gateway |
| Hard-set future/inconsistent timestamps | Generators corrected; harness validates consistency and rejects future history |
| Compound behavioral tests intercepted by velocity | Independent velocity allowance and genuinely combining signals in those cases |
| Fuzz cases affected subsequent cases | Separate agent context per case; category attacks are actually disallowed |
| Failed jobs counted as throughput success | Isolated benchmark counts successful/failed/pending separately and records environment/scope |
| Simulation claimed unused live reasoning | Removed unused LLM/graph dependencies; deterministic bounded simulator is described honestly |
| Attack amounts tied only to default cap | Generator accepts current policy cap; standard demo intent avoids unrelated semantic holds |
| Unsafe test database assumptions | Disposable test DB by default, explicit PostgreSQL `_test` guard, no provider keys, isolated Redis namespaces |
| Reset could target valuable installation | Explicit reset confirmation and production-profile prohibition; Docker-targeted runbook |
| Production container defaults | Non-root images, no reload/source mounts, read-only backend, internal DB/cache, persistent Redis AOF and local-only front door |
| Missing schema upgrades | Alembic adoption migration preserves records, reserves legacy approvals conservatively, disables legacy policies without a trusted configured goal |
| Misleading health/operations | Database/queue/worker checks, outbox task, payment/review/usage metrics and descriptive distribution monitoring |
| Human corrections never reusable | Scoped feedback export with original decision/reviewer and explicit history/label-review requirement |
| Unsupported AP2 authority | Unsigned HTTP authorization disabled with 501; translation helpers confer no public payment authority |
| Possible accidental live gateway use | Non-test Razorpay keys rejected |
| Old/unused vulnerable dependencies | Removed unnecessary LangChain/LangGraph runtime; updated and pinned runtime/development dependency closures; refreshed npm lock |

## Production runtime follow-up

The runtime now uses PostgreSQL for accepted jobs, releases agent locks and database connections during model IO, and rechecks policy/clock/budget before committing. Worker leases fence financial authorization as well as completion. Pending approvals expire or can be cancelled; current policy and authorizing credential scope are checked again before dispatch. Stale processing becomes unknown, late provider responses preserve captured state, and privileged emergency freeze works without Redis. Request bodies/admission, DB pools/timeouts, model concurrency, transports, recovery batches and shutdown are bounded. Historical migration schemas are frozen. The administrator UI supports reasoned pending cancellation. See [runtime design, upgrade and incident operations](PRODUCTION_READINESS.md).

## Executed verification

- **202 backend tests passed** on Ubuntu/Python 3.12 with PostgreSQL 16 and Redis 7. Coverage includes original authority/accounting behavior, two-phase semantic races, lease fencing, queue restart/retry/retention, approval expiry/cancellation, credential changes, stale provider effects, migrations and HTTP limits.
- **9 frontend tests passed**; lint and production build passed. Pending cancellation requires an explicit reason and targets the selected reservation.
- Python runtime dependency audit: **no known vulnerabilities found**. npm audit: **zero known vulnerabilities**. These are time-specific advisory checks, not proof of system security.
- **All 23 real container acceptance checks passed**: built UI, policy/approval/replay, changed replay rejection, hard cap, 24 concurrent HTTP purchases, freeze/cancel/resume, durable batch admission, Redis outage and recovery, PostgreSQL outage and recovery, continued service with one backend stopped, DNS refresh after backend recreation, and logical backup restoration into an isolated database.
- The measured HTTP smoke run completed **24/24 requests**, zero failures, 8 workers, p50 **109.18 ms**, p95 **133.65 ms**, about **65.02 requests/s**. The small logical restore exercise took **1.273 s**. Both use synthetic data and mock providers; neither is an SLA, production capacity claim, or production disaster-recovery target.
- [Successful implementation run](https://github.com/TheMS1314/sentinel/actions/runs/44044494272), commit `8a9fbc9`; [machine-readable evidence](verification/production-ci.json). The CI artifact contains the complete acceptance report, image identifiers, statuses and container logs.
- Current local full suite: **187 passed, 15 skipped** when Redis and local PostgreSQL were unavailable. All Redis/PostgreSQL variants ran successfully in CI. The earlier complete local run before the final additions passed 196 tests with three PostgreSQL-only skips.
- Synthetic evaluations were rerun against this runtime in explicit mock mode: golden **89/89**, hard **14/16** (two conservative holds), and fuzz **187 cases / zero selected invariant violations**. Updated reports are in `backend/data`. Earlier prevalence results remain historical, not a fresh run.
- No live OpenRouter/Anthropic, Razorpay test-provider, or licensed Mastercard/Recorded Future/RiskRecon calls were made. Full Compose execution was verified in GitHub CI, not on a production host. External TLS, HA infrastructure and production-provider contracts were not provisioned.
- The branch is published in PR #1. Main remains unmerged and no production deployment was performed.

## Risk intelligence follow-up

The branch additionally implements local threat evidence, owner-scoped vendor profiles, bounded merchant correlation and a separate emergency dispatch gate. These are Sentinel controls inspired by the named product categories, not commercial integrations. See [the design and boundaries](RISK_INTELLIGENCE.md). Added tests cover source/identity/owner boundaries, evidence expiry, review restrictions, changes after approval, replay, global concurrent dispatch limits, exact amounts, control audit/version conflicts and the administrator UI. New migrations are checked from fresh, legacy and already-hardened databases. PostgreSQL/Redis CI passed all 175 backend tests, including the 28 added intelligence/upgrade cases, and frontend CI passed all eight tests, lint, build and npm audit. The 89-case mock golden benchmark was rerun successfully. [Follow-up run](https://github.com/TheMS1314/sentinel/actions/runs/43947067184).

## Important remaining boundaries

1. This project deliberately rejects live payment keys. Verified merchant identity, customer payment capture integration, real financial operations and authenticated AP2 protocol support are not claimed.
2. Behavioral weights, confidence and distribution alert thresholds are heuristics. Representative labels and repeated live-provider experiments are required for calibration or performance claims. The code does not automatically train from reviewer actions.
3. A database and external provider cannot share one atomic transaction. Unknown effects stay reserved until a known order is reconciled. A provider idempotency/identity contract is still required before expanding beyond test orders.
4. Privileged database administrators remain trusted. Separate records and review events are auditable application history, not cryptographically tamper-proof storage.
5. Compose is a local topology, with shared dependencies and Redis AOF every second. TLS, managed identities/secrets, backups, distributed dependency failover and operator alert delivery belong to a deployment environment; they were not fabricated or provisioned during this task.
6. PostgreSQL and container CI verify the covered runtime, failure and migration paths. Live-provider contracts and a real production deployment still require their own evidence; passing CI does not establish production readiness.

## Review and publication

Review the source and test changes in [pull request #1](https://github.com/TheMS1314/sentinel/pull/1), branch `codex/sentinel-hardening`. The branch contains repository source, synthetic test/evaluation data, configuration examples, migration/CI definitions and documentation. Generated credentials, `.env` files, virtual environments, databases and logs are excluded from Git.

The branch is available in the public `TheMS1314/sentinel` repository with CI enabled. Publication does not merge to main or deploy the application.

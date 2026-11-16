# Sentinel hardening review

This branch replaces several prototype assumptions with explicit authority, accounting and recovery boundaries. The changes are published on `codex/sentinel-hardening` in [pull request #1](https://github.com/TheMS1314/sentinel/pull/1). Main remains unmerged.

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
| Redis pop lost claimed jobs | Consumer groups, pending-entry reclamation, lease renewal, atomic completion/ack and bounded atomic batch enqueue |
| Redis outage became unrelated local queues | Redis mode fails closed rather than silently falling back |
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

## Executed verification

- Backend CI: **147 tests passed** on Ubuntu/Python 3.12 with PostgreSQL 16 and Redis 7, including fresh and legacy Alembic upgrades, pending-entry recovery, atomic queue capacity, concurrent allowance/dispatch tests, HTTP access/session/CSRF tests, signed webhook handling and idempotency. [Successful run](https://github.com/TheMS1314/sentinel/actions/runs/43938151453) at commit `b3b7a52`. The earlier complete local suite passed 144 tests before the additional migration cases; local migration checks subsequently passed with the PostgreSQL variants skipped because no local server is available.
- Frontend: **4 tests passed**, both locally and in GitHub CI; lint clean; production build passed; CI npm audit passed.
- Runtime dependency audit: **no known vulnerabilities found** in the 47 pinned Python runtime packages at audit time. Frontend npm audit: **zero vulnerabilities**. This is a time-specific advisory check, not a security guarantee.
- Golden benchmark: **89/89**, synthetic prevalence: **500/500**, hard benchmark: **14/16**. These refreshed runs use mock reasoning; the hard misses are conservative holds where constructed labels expect approval. Safety counts and runtime/dataset fingerprints are saved.
- Fuzzing: **187 variants, zero selected invariant violations** with isolated contexts.
- Demo reset/seed: checked against a disposable database, never the user's application database.
- Compose configuration validated. Docker images and full deployment could not be started because the local Docker daemon is unavailable.
- PostgreSQL service/concurrency and fresh/legacy migration tests **passed in GitHub CI**. Local database tests use SQLite; the macOS sandbox prevents starting a separate PostgreSQL shared-memory server here. Concurrent database sessions exercise PostgreSQL advisory locking; a full container deployment and fault-injection campaign across replicas remain unverified.
- External OpenRouter/Anthropic and Razorpay test-provider calls were **not made**. Keys, external provider behavior and real signature configuration require separate integration verification.
- The branch is published and backend/frontend CI jobs passed. The initial service health-command quoting error was corrected and verified by subsequent runs. The latest main README edit was incorporated into this branch. No deployment or merge into main was performed.

## Risk intelligence follow-up

The branch additionally implements local threat evidence, owner-scoped vendor profiles, bounded merchant correlation and a separate emergency dispatch gate. These are Sentinel controls inspired by the named product categories, not commercial integrations. See [the design and boundaries](RISK_INTELLIGENCE.md). Added tests cover source/identity/owner boundaries, evidence expiry, review restrictions, changes after approval, replay, global concurrent dispatch limits, exact amounts, control audit/version conflicts and the administrator UI. New migrations are checked from fresh, legacy and already-hardened databases. PostgreSQL validation of this follow-up is recorded after CI completes.

## Important remaining boundaries

1. This project deliberately rejects live payment keys. Verified merchant identity, customer payment capture integration, real financial operations and authenticated AP2 protocol support are not claimed.
2. Behavioral weights, confidence and distribution alert thresholds are heuristics. Representative labels and repeated live-provider experiments are required for calibration or performance claims. The code does not automatically train from reviewer actions.
3. A database and external provider cannot share one atomic transaction. Unknown effects stay reserved until a known order is reconciled. A provider idempotency/identity contract is still required before expanding beyond test orders.
4. Privileged database administrators remain trusted. Separate records and review events are auditable application history, not cryptographically tamper-proof storage.
5. Compose is a local topology, with shared dependencies and Redis AOF every second. TLS, managed identities/secrets, backups, distributed dependency failover and operator alert delivery belong to a deployment environment; they were not fabricated or provisioned during this task.
6. PostgreSQL CI verifies the covered database, concurrency and migration paths. External model/provider integrations and full deployment still require their own execution evidence; passing CI does not establish production readiness.

## Review and publication

Review the source and test changes in [pull request #1](https://github.com/TheMS1314/sentinel/pull/1), branch `codex/sentinel-hardening`. The branch contains repository source, synthetic test/evaluation data, configuration examples, migration/CI definitions and documentation. Generated credentials, `.env` files, virtual environments, databases and logs are excluded from Git.

The branch is available in the public `TheMS1314/sentinel` repository with CI enabled. Publication does not merge to main or deploy the application.

# Implementation audit and release limits

This pass reviews authority, retries, financial state, dependency boundaries and executable deployment checks. It fixes the concrete findings below. It does not establish that every possible production failure has been discovered or that a production release is approved. Main remains unmerged.

| Finding | Implementation | Verification |
|---|---|---|
| Replica clocks could disagree on credential/session expiry, review budgets and recovery timestamps | Shared authority checks, browser-session issuance/expiry, current human-review clock, ledger processing timestamps, job admission and payment-state timestamps use the database clock. Payment/job ORM automatic host-clock updates are removed; runtime transitions set authoritative timestamps explicitly. | Credential/session skew regressions, durable admission timestamps, full runtime suite |
| An expired worker could renew its old lease before takeover | Renewal and completion require both a matching token and a still-live lease. | Expiry-before-takeover and winner-recovery regression |
| A previously attempted job escaped the submission-age limit | Every queue claim applies the original command deadline. Final authorization rechecks that deadline even while a worker lease is live. A failed job never proves that an external payment did not occur; inspect its durable request before new work. | Retried-command expiry and live-lease/expired-command regression |
| A mismatched risk-profile domain could soften an existing BLOCK and erase reasons | Enrichment preserves the stricter verdict and retains prior reasons and profile-domain threat provenance. Clearing obsolete evidence requires an explicit operator update. | Canonical merchant/domain mismatch regression with a confirmed threat and no payment reservation |
| Provider response identity/schema was weakly checked | Order creation checks reference shape, integer amount, INR, request/merchant notes, receipt and initial state. Reconciliation checks the fetched ID and known states. Mismatch remains unknown/reserved, with no automatic order retry. Raw provider exception content is omitted from gateway logs. | Response-corruption matrix, response-ID mismatch, unknown outcome and log-redaction regressions |
| Optional caches had no memory bound or explicit Redis transport bound | Memory cache uses monotonic expiry, bounded LRU eviction and expired-entry cleanup. Redis cache connections/timeouts and scan pages are bounded; incomplete counts are marked truncated. Error logs omit raw exceptions and cache keys. | LRU/expiry, scan bounds, Redis configuration and sensitive-error regressions |
| Kubernetes dependency TLS and storage survived only structural review | Disposable CI uses authenticated, CA/hostname-verified SQL/Redis clients, rejects plaintext/untrusted CAs and replaces the PostgreSQL pod while retaining its PVC-backed ledger. | Actual images, migrations, grants and HTTPS application exercise; production storage/HA is still a separate release gate |

The database clock itself must be maintained correctly by the production platform. These changes remove independent replica-clock authority; they do not prove clock synchronization across a real database failover.

## Open upstream development dependency advisory

GHSA-fc5q-39xc-vjpf (CVE-2026-112366) affects picomatch through 4.0.7. The upstream advisory and registry listed no patched release when reviewed on 2026-11-17. This is **not fixed** by this branch.

The affected packages are development/build/test tooling in the lockfile. The deployed frontend image contains nginx and compiled assets, with no Node development server. Vite development/preview servers are explicitly loopback-only, and project glob patterns come from repository configuration. CI jobs have bounded runtimes and read-only repository permissions. Do not expose build/dev tools to untrusted glob inputs or run unreviewed code with deployment credentials.

`scripts/check_frontend_audit.py` checks both full and production-only npm audit reports. It permits this one exact advisory only when every affected lockfile node is development-only and the production report has zero findings. It emits a visible warning and saves the full reports; it never reports a clean development audit. Unknown findings, broken audit reports, a new upstream picomatch release or the **2026-12-01 UTC** expiry block CI. npm may suggest incompatible parent-tool downgrades through `audit fix --force`; those are not treated as upstream picomatch patches and are not applied automatically. The exception can be removed after supported dependency updates and verification.

References:

- https://github.com/advisories/GHSA-fc5q-39xc-vjpf
- https://registry.npmjs.org/picomatch/latest
- https://www.postgresql.org/docs/16/ssl-tcp.html
- https://redis.io/docs/latest/operate/oss_and_stack/management/security/encryption/

## Remaining release gates

Licensed provider/model/test-payment contracts, independent representative labels, an actual alert recipient, public TLS/issuer renewal, real secret operations, a NetworkPolicy-enforcing CNI, managed-service/multi-zone failover and a production restore/reconciliation drill still need environment-specific proof. A disposable PVC pod-replacement test is not cross-zone storage recovery. Real-money settlement and authenticated AP2 remain unsupported. See [the gap register](GAP_REGISTER.md).

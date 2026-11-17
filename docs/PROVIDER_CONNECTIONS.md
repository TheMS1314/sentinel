# Commercial enrichment boundaries

The Providers tab/API stores administrator-reviewed mappings and refreshes minimized, append-only evidence outside payment authorization. Raw licensed responses and tokens are not stored. Fixed HTTPS origins, no redirects or environment proxies, 3s connect/5s read timeouts, two concurrent refresh slots, no automatic request retries and a 256KiB body ceiling bound transport. Refresh errors are persisted explicitly; no mock response becomes commercial evidence. Private merchant domains are disclosed to the selected licensed service when an administrator refreshes a binding.

Implemented read-only adapters, based on the official API reference consulted 17 November 2026:

| Adapter | Endpoint | Credential | Validated subset |
|---|---|---|---|
| Recorded Future | `https://api.recordedfuture.com/v2/domain/{id}` with `entity,risk,timestamps` | `RECORDED_FUTURE_API_TOKEN`, X-RFToken | `data.entity.id`, `data.risk.score` in 0–99, `data.timestamps.lastSeen` with timezone |
| RiskRecon sandbox | `https://api.sandbox.riskrecon.com/v0/portfolio/member/toe_id/{toe_id}` | `RISKRECON_API_TOKEN`, Bearer | One matching `toes[].toe_id`, `overall_rating_numeric` in 0–10, `last_analysis_ts` with timezone |

References: [Recorded Future overview](https://docs.recordedfuture.com/reference/get-started), [domain lookup](https://docs.recordedfuture.com/reference/domain-lookup), [field attributes](https://docs.recordedfuture.com/reference/enrichment-field-attributes), [RiskRecon portfolio member](https://docs.recordedfuture.com/reference/customerapiswebv0portfoliocontrollerportfolio_member_by_analysis_id).

These are strict adapter subset contracts, tested with **synthetic fixtures**, not captured licensed responses. Actual entitlements and response variants must pass the opt-in integration check before rollout. Unrecognized variants fail to error/HOLD; do not loosen validation merely to mark a request successful. RiskRecon production origin is deliberately not guessed from the public sandbox documentation. Operator mapping of a TOE is an assertion, not independent ownership proof.

Set `SENTINEL_REQUIRED_FEEDS=recorded_future,riskrecon` to require coverage. Every explicitly bound feed is also enforced. No mapping/evidence, refresh error, version mismatch, expiry or elevated score causes HOLD. Absence of optional enrichment is recorded as absence and never called a safe rating. A refreshed record cannot outlive either its fetch TTL (default 1h) or source age limit (default 7 days). Recorded Future last-seen measures source activity; conservative age expiry can require review for quiet domains. Bindings can be refreshed manually or through the explicitly opted-in leased scheduler described below; the authorization hot path performs no commercial network IO. Refresh after expiry is necessary to resume automated dispatch.

Provider scores retain their native scales. Recorded Future >=70 or RiskRecon <5 requires review; these are disclosed, uncalibrated thresholds. They are not fraud probabilities. Identity/binding version changes invalidate cached evidence. Evidence can restrict authority, never relax policy. The dispatch path checks it again.

Mastercard Decision Intelligence and Safety Net are network services requiring commercial onboarding and supported issuer/payment rails. Sentinel includes its own explainable transaction signals and emergency guard, and does not claim either proprietary service is connected or replicated. Real-money settlement remains disabled; Razorpay integration remains TEST orders only.

## Scheduled refresh and lease fencing

Automatic refresh is opt-in at **both** deployment and binding level. Start the bounded worker with `SENTINEL_PROVIDER_REFRESH_ENABLED=1`, then explicitly enable a binding in Providers. Default poll interval is 15s; each replica processes at most two attempts per cycle sequentially, while adapter concurrency is separately capped. Binding intervals default to 300s and accept 60–86400s. Set cadence/concurrency to the actual licensed quota; no network-wide entitlement or throughput is assumed. Missing credentials are recorded as `not_configured`, followed by bounded delayed retries, never synthetic success.

Manual and automatic refresh share a PostgreSQL lease per binding. A completed claim transaction releases its lock and connection before IO. A crashed worker's lease expires (default 60s), allowing another replica or a manual request to recover it. A random token plus monotonically increasing generation fences completion: an expired or superseded worker cannot append evidence or replace newer results. Indexed generations order current evidence independently of host clocks. Failed attempts cannot erase the highest validated source timestamp for that binding/identity version. Expiry and future source dates are rechecked against the database clock at completion.

Every mapping is bound to the version of the registered merchant that an administrator reviewed. Any identity version change requires an explicit mapping review/save before **either** provider can be queried again. Background refresh cannot silently reattest an old RiskRecon TOE or disclose an obsolete Recorded Future domain. Edits invalidate in-flight results; a request already claimed may still finish its read at the provider, but its response cannot become current evidence after the mapping changes.

Revision `0008_provider_refresh` preserves evidence and leaves existing bindings opted out. Their mapping version is deliberately unreviewed (`0`): resave each mapping after checking the current identity before refreshing again. Merely applying a migration never initiates licensed API calls.

Providers shows the worker's enabled/disabled state, per-binding opt-in, interval, next attempt, last result, active lease and remapping requirement. Operations opens durable cases for unavailable scheduled evidence or a stalled worker. Such cases require administrator resolution after the condition clears. An enrichment-worker failure is reported without removing an otherwise usable API from service; existing per-feed expiry/HOLD checks remain authoritative. Operator-provided input and licensed live integration proof remain separate gates.

# Commercial enrichment boundaries

The Providers tab/API stores administrator-reviewed mappings and refreshes minimized, append-only evidence outside payment authorization. Raw licensed responses and tokens are not stored. Fixed HTTPS origins, no redirects or environment proxies, 3s connect/5s read timeouts, two concurrent refresh slots, no automatic request retries and a 256KiB body ceiling bound transport. Refresh errors are persisted explicitly; no mock response becomes commercial evidence. Private merchant domains are disclosed to the selected licensed service when an administrator refreshes a binding.

Implemented read-only adapters, based on the official API reference consulted 17 November 2026:

| Adapter | Endpoint | Credential | Validated subset |
|---|---|---|---|
| Recorded Future | `https://api.recordedfuture.com/v2/domain/{id}` with `entity,risk,timestamps` | `RECORDED_FUTURE_API_TOKEN`, X-RFToken | `data.entity.id`, `data.risk.score` in 0–99, `data.timestamps.lastSeen` with timezone |
| RiskRecon sandbox | `https://api.sandbox.riskrecon.com/v0/portfolio/member/toe_id/{toe_id}` | `RISKRECON_API_TOKEN`, Bearer | One matching `toes[].toe_id`, `overall_rating_numeric` in 0–10, `last_analysis_ts` with timezone |

References: [Recorded Future overview](https://docs.recordedfuture.com/reference/get-started), [domain lookup](https://docs.recordedfuture.com/reference/domain-lookup), [field attributes](https://docs.recordedfuture.com/reference/enrichment-field-attributes), [RiskRecon portfolio member](https://docs.recordedfuture.com/reference/customerapiswebv0portfoliocontrollerportfolio_member_by_analysis_id).

These are strict adapter subset contracts, tested with **synthetic fixtures**, not captured licensed responses. Actual entitlements and response variants must pass the opt-in integration check before rollout. Unrecognized variants fail to error/HOLD; do not loosen validation merely to mark a request successful. RiskRecon production origin is deliberately not guessed from the public sandbox documentation. Operator mapping of a TOE is an assertion, not independent ownership proof.

Set `SENTINEL_REQUIRED_FEEDS=recorded_future,riskrecon` to require coverage. Every explicitly bound feed is also enforced. No mapping/evidence, refresh error, version mismatch, expiry or elevated score causes HOLD. Absence of optional enrichment is recorded as absence and never called a safe rating. A refreshed record cannot outlive either its fetch TTL (default 1h) or source age limit (default 7 days). Recorded Future last-seen measures source activity; conservative age expiry can require review for quiet domains. Existing bindings must be refreshed by an operator/scheduler; the authorization hot path performs no commercial network IO. Refresh after expiry is necessary to resume automated dispatch.

Provider scores retain their native scales. Recorded Future >=70 or RiskRecon <5 requires review; these are disclosed, uncalibrated thresholds. They are not fraud probabilities. Identity/binding version changes invalidate cached evidence. Evidence can restrict authority, never relax policy. The dispatch path checks it again.

Mastercard Decision Intelligence and Safety Net are network services requiring commercial onboarding and supported issuer/payment rails. Sentinel includes its own explainable transaction signals and emergency guard, and does not claim either proprietary service is connected or replicated. Real-money settlement remains disabled; Razorpay integration remains TEST orders only.

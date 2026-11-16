# Sentinel risk intelligence: first implementation

Sentinel now has four complementary layers inspired by payment-risk systems. These are Sentinel implementations, not Mastercard products or connected commercial services.

| Inspiration | Implemented in Sentinel | Boundary |
|---|---|---|
| Decision Intelligence | Existing per-agent behavioral score plus bounded, owner-scoped correlation of several agents targeting a configured merchant in five minutes | Descriptive heuristics; no trained fraud model, network-wide graph, or calibrated probability |
| Recorded Future | Exact-domain matching against expiring, attributed, administrator-managed threat indicators | Local operator/synthetic evidence; no live Recorded Future feed |
| RiskRecon | Owner-scoped merchant/domain directory, attributed cyber-exposure scores, evidence and expiry | Manual posture records; no security scanning, independent merchant verification, or RiskRecon ratings |
| Safety Net | A separate payment-dispatch gate with global freeze and rolling count/amount limits | Same application/database, not an independently operated network-level defense |

## Decision flow

1. Authenticate the caller and apply owner policy, exact-money, time, budget and intent restrictions.
2. Apply existing behavioral scoring. Hard policy blocks remain authoritative.
3. Enrich non-blocked decisions with vendor, threat and relation signals. Store the complete original assessment with the request.
4. Confirmed operator evidence matching an operator-configured merchant domain blocks. Synthetic or unconfirmed matches, stale evidence and vendor cyber scores of at least 70 hold. These thresholds are uncalibrated. Clean or missing enrichment never grants payment authority.
5. An approved transaction commits its payment reservation/outbox entry atomically.
6. Immediately before claiming payment dispatch, recheck the emergency guard and current confirmed threat evidence under one global PostgreSQL advisory transaction lock.
7. Persist the dispatch claim and the payment's processing state together, release the lock, then call the test payment provider.

A PASS enrichment result means only that this layer added no restriction. Unknown identity and absent local threat matches are explicitly unknown/partial coverage, not evidence of merchant safety. Merchant names remain caller assertions; a renamed merchant can evade a name-based directory. Trusted merchant identity from a payment/acquirer contract is required before relying on these checks for real money.

## Evidence and matching

Administrators create merchant/domain bindings for a specific owner. Merchant keys use Unicode NFKC normalization, case folding and collapsed whitespace. Domains accept exact IDNA hostnames, normalize case/trailing dots, and reject URLs, IP addresses, wildcards and ports. Subdomains and suffixes are not assumed equivalent. Sentinel performs no arbitrary URL fetching or scanning.

Threat records are global administrator-owned indicators. They affect a decision only through its owner's merchant profile. Both the mapping and indicator must be operator evidence for a hard block; synthetic evidence produces a hold. Each record has a source, evidence/reason, author, update timestamp and expiry. Expired matching evidence produces a review signal, never a fresh confirmed match. Commercial provider names cannot be submitted as invented local sources.

The relation check considers at most 500 recent events, from the same owner's agents, using server processing time. It triggers when at least four prior attempts plus the current request involve three or more agents and the same configured merchant within five minutes. The assessment records truncation. This is a coordination warning, not a proven fraud finding; owner accounts are not correlated with each other. Registering a merchant profile enables this check for that merchant.

## Emergency guard semantics

- Only administrators change the guard. Changes require the last seen version; stale updates return 409 instead of overwriting a newer control.
- A global freeze prevents new dispatch claims. It cannot recall an order claimed before the freeze committed, or cancel an already created/captured provider order.
- Rolling count and amount limits apply across agents and owners. Amounts use integer paise.
- Failed and unknown dispatch attempts still consume the guard's window allowance. Retrying a stable request does not create another claim.
- A paused payment remains `pending` with `guard_hold` on its decision/provider status and a reason on its ledger record. Its approved budget remains reserved.
- The outbox periodically retries pending rows. Changing the guard or removing a current confirmed threat can allow a previously paused payment to dispatch automatically. Operators should review pending payments before unfreezing.
- Recovery orders pending payments by their last attempt time; paused rows move to the back of the recovery queue so they do not permanently starve other pending work.
- Database administrators remain trusted. Control changes record actor and before/after state in `risk_control_events`; this is an application audit trail, not immutable cryptographic storage.

A human may approve a heuristic HOLD, subject to the normal current policy checks. Current confirmed threats cannot be overridden through the hold-review endpoint. The dispatch gate applies even after human approval. The original intelligence snapshot stays unchanged; current evidence may therefore pause a payment whose historical snapshot was clear.

## Use

Run migrations before starting an existing installation:

```bash
docker compose run --rm db-init
```

Sign in as an administrator and open **Risk intelligence**. The dashboard shows provider status, evidence counts, pending payments, guard controls, manual evidence forms and recent control changes. Transaction details show the original intelligence coverage and signals. The initial guard has no freeze/aggregate limits; configure them explicitly for a deployment. Individual agent policy limits still apply.

| Route | Access | Purpose |
|---|---|---|
| `GET /risk/overview` | Admin | Provider status, guard, evidence counts, recent control events |
| `POST /risk/guard` | Admin | Versioned freeze and rolling limits, with reason |
| `GET/POST /risk/merchants` | Admin | List up to 200 recent profiles; create/update by owner and normalized name |
| `GET/POST /risk/indicators` | Admin | List up to 200 recent indicators; create/update/deactivate by indicator ID |
| `GET /risk/assessments/{request_id}` | Scoped caller | Original decision intelligence snapshot |

With Compose use the `/api` prefix. Cookie mutations use existing origin/CSRF protection. Service callers use scoped API credentials. Evidence expiry must be a future timezone-aware timestamp. Profile/indicator changes are serialized with dispatch claims and audited.

Example guard body (₹1,000 aggregate attempted dispatches per minute):

```json
{
  "expected_version": 0,
  "frozen": false,
  "window_seconds": 60,
  "max_dispatches": 20,
  "max_amount_paise": 100000,
  "reason": "Initial local test limits"
}
```

## Commercial integration path

Recorded Future documents API token authentication with data/endpoint entitlements determined by licensed modules. RiskRecon documents separately subscribed API access. Mastercard Decision Intelligence and Safety Net require their respective product onboarding and an appropriate payment-system role; this project does not possess a Mastercard network vantage point.

A later licensed connector should run asynchronously: fetch only entitled evidence, validate entity identity and schemas, record provider/observation/fetch times and raw-response hashes, enforce expiry/rate/timeout limits, and publish normalized snapshots. Unavailable or stale required feeds must produce an explicit review state. External intelligence must never override the owner's hard restrictions. No transaction data is sent to these providers by this implementation.

Next additions with clear value are trusted merchant identifiers, provider connectors, case lifecycle/incident workflows, notifications, shadow evaluation with time-split labels, and calibrated transaction models with rollback. Cross-tenant entity graphs need explicit data rights and privacy boundaries. Adding another model without representative labels would not establish better fraud detection.

Official product references:

- [Mastercard Decision Intelligence](https://www.mastercard.com/global/en/business/cybersecurity-fraud-prevention/risk-decisioning/decision-intelligence.html)
- [Mastercard Safety Net: independent safety layer](https://investor.mastercard.com/investor-news/investor-news-details/2015/MasterCard-Enhances-CyberSecurity-Peace-of-Mind/default.aspx)
- [Recorded Future API overview and entitlements](https://docs.recordedfuture.com/reference/get-started)
- [RiskRecon service and API subscription description](https://www.mastercardservices.com/en/cyber-security-era-ai-terms-conditions-submission-page)

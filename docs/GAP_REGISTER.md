# Remaining gap register

This register distinguishes implemented software, executed checks and external release blockers. A passing mock, schema check or deployment template does not close a live integration or infrastructure gate. Main remains unmerged.

| ID | Gap | Software completion target | External proof needed | Current state |
|---|---|---|---|---|
| G01 | Per-replica configured credentials can drift | Database-backed scoped credentials, immediate shared revocation/rotation, audited administrator operations, safe bootstrap | Deployment identity/SSO integration if customer sign-in is required | Implemented; local tests pass, CI pending |
| G02 | Agent-asserted merchant names/categories | Owner-scoped stable merchant IDs, server-owned attributes, lifecycle/version checks at authorization and dispatch | Acquirer/provider proof and settlement binding before real money | In progress |
| G03 | Monitoring has no delivered alerts or incident workflow | Durable alert delivery with retries, administrator incident lifecycle, protected metrics/export, tested sink | Configure and verify an actual operational recipient | Open |
| G04 | Named commercial services are disconnected | Documented API adapters with schema validation, freshness/provenance and bounded transport; explicit unconfigured/error states | Licensed access to Recorded Future/RiskRecon/Mastercard as applicable | Open; credentials/entitlements unavailable |
| G05 | Deployment topology and database privileges are only local | Restricted runtime DB grants, migration separation, TLS/HA deployment configuration and automated validation | Real cluster/account/domain, managed database/cache HA and failover exercise | Open |
| G06 | Real model/payment-provider calls have not run | Opt-in bounded contract tests and explicit integration verification status, no live payment keys | Actual model/test-provider credentials and successful run | Open; credentials unavailable |
| G07 | Heuristics have no representative calibration evidence | Curated feedback-to-evaluation tooling, time-separated evaluation and calibration/report provenance without automatic authority expansion | Independently reviewed representative labels and live-provider evaluations | Open; representative data unavailable |

Already executed: 202 backend tests, nine frontend tests and 23 real mock-provider Compose acceptance checks, including PostgreSQL/Redis outages, backend recreation and logical restoration. See HARDENING_REPORT.md for the recorded implementation run. New work below must get its own evidence.

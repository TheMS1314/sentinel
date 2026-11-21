# Opt-in provider contract verification

`app.ops.verify_integrations` reports each requested target as `verified`, `failed` or `blocked`; missing credentials or mock output never become a passing live check. Without `--execute`, it sends no request. Reports omit tokens, raw model output and licensed response bodies. Source revision/dirty state and runtime mode are retained.

```sh
PYTHONPATH=backend python -m app.ops.verify_integrations --execute --targets model,razorpay_test,recorded_future,riskrecon --output artifacts/integration-contracts.json
```

This may consume bounded model tokens and create one INR1 **Razorpay TEST order**. It never captures money or accepts live payment keys. A timeout/unknown order is a failed check requiring sandbox reconciliation before retrying, not proof nothing happened. A successful order/create/fetch probe does not verify customer checkout, capture, webhook delivery, refund behavior or real settlement.

| Target | Configuration | Successful contract proves |
|---|---|---|
| `model` | `LLM_PROVIDER`, explicit `LLM_MODEL`, matching `OPENROUTER_API_KEY` or `ANTHROPIC_API_KEY` | A real provider returned the strict synthetic structured-output schema; no accuracy claim |
| `razorpay_test` | `RAZORPAY_KEY_ID` beginning `rzp_test_`, `RAZORPAY_KEY_SECRET` | One test order was created and fetched with matching request metadata, amount and currency |
| `recorded_future` | `RECORDED_FUTURE_API_TOKEN`, authorized `SENTINEL_PROBE_DOMAIN_ID` such as `idn:domain.example` | Licensed read passed entity/schema/source-age checks |
| `riskrecon` | `RISKRECON_API_TOKEN`, authorized `SENTINEL_PROBE_RISKRECON_TOE` | Sandbox portfolio-member read passed schema/source-age checks |

Supply credentials through the runtime secret manager, not CLI arguments or public files. Use a synthetic/approved probe domain or TOE; enrichment discloses the entity to the selected service. A local check during this task returned **blocked for all four targets**, with no live requests, because usable credentials/probe entities were absent. The connected GitHub interface cannot read secret values or secret metadata; repository secret availability has not been verified.

`.github/workflows/integration-contracts.yml` provides a manually triggered workflow with redacted artifacts and named repository secrets/variables. GitHub requires a workflow-dispatch definition on the default branch before it appears for manual dispatch. Main remains unmerged, so this new workflow has not been dispatched; the CLI is usable on the hardening branch. This distinction is deliberate: green offline CI is not a verified commercial/model/payment integration.

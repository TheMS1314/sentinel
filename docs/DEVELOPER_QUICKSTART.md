# Verify your first Sentinel payment flow

Run the example from the repository root with Python 3.12. The test creates its own database and uses mock reasoning and payment providers. You can run it without starting Docker or configuring provider keys.

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -r backend/requirements-dev.txt
.venv/bin/python -m pytest backend/tests/test_developer_quickstart.py -q --timeout=45
```

The [runnable example](../backend/tests/test_developer_quickstart.py) calls the same application routes as an integration. The URLs below are relative to the API. Prefix them with `/api` when calling through the Compose gateway. Send the role's credential in the `X-API-Key` header and use a unique `request_id` for each new proposal.

| Step | Request and expected output | Budget after the step |
|---|---|---|
| Create a policy | Owner posts to `/policies` with a ₹60 per-payment cap, ₹100 daily cap, grocery category, and the goal `buy weekly groceries`. Set `allowed_merchants` to `["Corner Market", "Example Shop"]` and `blocked_merchants` to `["Example Shop"]`. | ₹100 available |
| Approve a purchase | Agent posts a ₹40 proposal at Corner Market to `/transactions/evaluate` with intent `buy weekly groceries`. Expect HTTP 200 and `verdict: APPROVE`. Repeating the identical request ID and payload returns the same decision. | ₹40 reserved, ₹0 captured, ₹60 available |
| Block a purchase | Send a ₹60 proposal at Example Shop. Expect HTTP 200, `verdict: BLOCK`, and `merchant_blocklist` in `checked_rules`. Formatting variants keep the same result. A punctuation variant outside the allowlist fails `merchant_allowlist`. | ₹40 reserved, ₹0 captured, ₹60 available |
| Review a hold | Send ₹60 at Corner Market with intent `restock pantry supplies`. Expect `HOLD`. A reviewer posts `{"human_verdict":"APPROVE","reason":"Verified grocery request"}` to `/holds/{request_id}/resolve`. Current limits still apply. The agent gets HTTP 403 for the same review and a duplicate reviewer submission gets HTTP 409. | ₹60 available before review. After approval, ₹100 reserved and ₹0 available |
| Check the limit and evidence | Submit another ₹1 proposal. Expect `BLOCK` and `max_daily_total`. Read `/transactions?agent_id={agent_id}&limit=20` to see the original verdict, policy version, review decision and provider status. The reviewed row still shows `HOLD` with `human_override: APPROVE`. | ₹100 reserved, ₹0 captured, ₹0 available |

An HTTP 200 on evaluation means the API recorded a decision. Inspect the verdict and ledger before treating it as a successful payment.

## How to read the budget

`GET /policies/{agent_id}/budget` uses the same ledger calculation as authorization and the same access scope as the policy. It returns the current UTC budget day, policy version, and integer paise values for the cap, outstanding reservations, captures, total commitments and available allowance. Requests outside the credential's agent scope return HTTP 403. The budget is a snapshot and each new authorization rechecks current state.

After the first ₹40 mock approval, `daily_cap_paise` is 10000, `reserved_paise` and `committed_paise` are both 4000, `captured_paise` is 0 and `available_paise` is 6000. A ₹60 block changes none of those amounts. If 4000 paise were later confirmed captured, those 4000 paise would move from reserved to captured and the committed total would stay at 4000.

| Decision or ledger state | Reserved allowance | Captured amount |
|---|---|---|
| Proposed, blocked or unresolved hold | None | None |
| Approved and awaiting dispatch, or claimed and processing | Held | None |
| Mock order, test order created, or provider outcome unknown | Held | None |
| Confirmed capture | Reservation converted to capture | Counts on the approval's UTC budget day |
| Cancelled before dispatch, expired before dispatch, or confirmed failure | Released | None |

Blocked attempts still count toward velocity limits. An uncertain provider response keeps the reservation held until reconciliation. A failed job does not establish the outcome of a payment.

## How merchant matching works

Policy lists, behavioral scoring and local risk profiles use the same Unicode NFKC comparison, case folding and whitespace collapse. For example, `Example Shop`, `EXAMPLE   SHOP` and a fullwidth rendering of the same letters compare equal. Punctuation stays significant, so `Example-Shop` is a different name. Use an allowlist when only specific names are permitted.

For production-profile requests, register an owner-scoped merchant and submit its `merchant_id`. Sentinel evaluates the directory's canonical name and category even if a request supplies different display values. The directory records operator attestation. Provider and settlement verification remain separate.

# Review feedback and offline evaluation

A reviewer override is a business decision, not automatically an independent training label. `/feedback/cases` exports scoped candidates with original policy/request, trusted event time, reviewer, immutable identity/assessment snapshots and `requires_history_review=true`. The harness rejects uncurated exports. No export, calibration proposal or evaluation changes production policy, credentials, score weights or model behavior.

## Curation

An independent curator supplies a JSON array with one record per selected candidate:

```json
{
  "case_id": "review_REQUEST_ID",
  "verdict": "APPROVE",
  "curator": "INDEPENDENT_CURATOR",
  "rationale": "Evidence and context supporting the adjudicated label",
  "reviewed_at": "2026-11-17T09:00:00+00:00",
  "history_reviewed": true,
  "context_reviewed": true,
  "history_records": []
}
```

The curator must differ from the original reviewer. An explicit empty history list asserts that the case has no relevant history; it is not an automatic assumption. For each prior event, `history_records` carries its full transaction `request`, original `verdict`, optional `human_override`, observed `payment_state` and optional UTC `budget_day`. States must reflect the ledger as observed before the target event. The tool checks same-agent/unique/past chronology and authorization consistency. Curation still depends on a human checking completeness and correctness; validation cannot establish that evidence was truthful or representative.

```sh
PYTHONPATH=backend python -m app.eval.feedback_curation curate --exports PRIVATE_EXPORT.json --labels PRIVATE_LABELS.json --cutoff 2026-11-01T00:00:00+00:00 --output-dir artifacts/curated
PYTHONPATH=backend python -m app.eval.feedback_experiment --curated-dir artifacts/curated --output-dir artifacts/feedback-experiment --require-live
```

Omit `--require-live` only for a clearly labeled synthetic/mock workflow check. A required live run fails when credentials are absent. Split membership uses trusted source event time; both splits must contain reviewed cases. Independent adjudication is recorded with source/label hashes and curator/time metadata. Curated files and reports contain private request/case data and are written with owner-only permissions; keep them out of the public repository.

## Replay scope and experiments

The isolated harness replays **recorded ledger states**, including unknown reservations and human-approved holds, rather than re-deciding historical payments using the current policy. Operator identity snapshots are restored only inside a private disposable SQLite directory; no snapshot import is accepted by production APIs. Payments are always mocked in evaluations, even with live reasoning enabled. Each case has a distinct agent namespace and no future-history access.

Supported feedback replay covers single-agent policy/ledger/behavioral/semantic decisions and operator merchant identities. Summarized commercial enrichment, local vendor/threat evidence or triggered multi-agent correlations cannot reconstruct complete historical inputs. Such candidates are rejected for separate scenario reconstruction instead of being given invented history or clean evidence. Keep those cases for human investigation and explicit scenario tests; do not claim the feedback dataset covers them. The curation workflow validates a reviewed scenario; it does not certify a lossless replay of all production state.

The experiment runs chronological calibration/test splits in separate processes with retrieval enabled and disabled. It records dataset hashes, software revision/dirty state, prompt version, model, runtime mode, classification metrics, unsafe approvals and unnecessary holds. It then produces a **detector threshold proposal** chosen only from calibration data and reports it on the held-out split. Missing risk scores are excluded explicitly, never replaced with zero. The 10:1 error cost is illustrative, not an empirically established business cost. A composite score is not a probability; this does not establish probability calibration. No threshold is automatically applied. Compare proposals only under matching model/prompt/runtime settings.

The checked-in tests use constructed cases to verify chronology, independent adjudication, replay, leakage rejection and ablation orchestration. Representative adjudicated data, blinded review, suitable sample sizes, real class prevalence and repeated live-model evaluations remain external requirements. No synthetic result establishes real fraud accuracy or safe production thresholds.

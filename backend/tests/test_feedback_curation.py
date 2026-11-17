"""Independent labels, chronology and recorded-ledger replay protect evaluation claims."""

from copy import deepcopy
import json
import os
from pathlib import Path
import subprocess
import sys
import pytest
from app.eval.feedback_curation import curate, threshold_report
from app.models.policy import AgentPolicy, TransactionRequest


def example():
    p = AgentPolicy("p", "a", "owner", max_daily_total=15)
    request = TransactionRequest(
        "r",
        "a",
        "Shop",
        "grocery",
        10,
        12,
        "2026-01-02T12:00:00+00:00",
        "buy weekly groceries",
    )
    base = dict(
        case_id="review_1",
        category="human_feedback",
        policy=p.to_dict(),
        request=request.__dict__,
        source_created_at="2026-01-02T12:00:00+00:00",
        reviewer="first-reviewer",
        expected_verdict="APPROVE",
        requires_history_review=True,
        intelligence_snapshot={},
    )
    second = deepcopy(base)
    second["case_id"] = "review_2"
    second["request"]["request_id"] = "r2"
    second["request"]["requested_at_iso"] = "2026-02-02T12:00:00+00:00"
    second["source_created_at"] = "2026-02-02T12:00:00+00:00"
    labels = [
        dict(
            case_id=r["case_id"],
            verdict="APPROVE",
            curator="independent-person",
            rationale="Reviewed context",
            reviewed_at="2026-03-01T00:00:00+00:00",
            history_reviewed=True,
            context_reviewed=True,
            history_records=[],
        )
        for r in (base, second)
    ]
    return [base, second], labels


def test_curation_splits_by_trusted_event_time_and_does_not_reuse_override_blindly():
    exports, labels = example()
    labels[1]["verdict"] = "BLOCK"
    result = curate(exports, labels, "2026-02-01T00:00:00+00:00")
    assert result["calibration"][0]["expected_verdict"] == "APPROVE"
    assert result["test"][0]["expected_verdict"] == "BLOCK"
    assert result["test"][0]["curation"]["source_case_hash"]
    assert exports[1]["requires_history_review"] is True


@pytest.mark.parametrize(
    "bad",
    [
        "same_reviewer",
        "no_history_review",
        "future_history",
        "summarized_provider",
        "missing_identity",
    ],
)
def test_invalid_or_unreplayable_candidates_cannot_silently_enter_dataset(bad):
    exports, labels = example()
    if bad == "same_reviewer":
        labels[0]["curator"] = "first-reviewer"
    if bad == "no_history_review":
        labels[0]["history_reviewed"] = False
    if bad == "future_history":
        prior = deepcopy(exports[0]["request"])
        prior["request_id"] = "history"
        prior["requested_at_iso"] = "2026-10-01T12:00:00+00:00"
        labels[0]["history_records"] = [
            dict(request=prior, verdict="APPROVE", payment_state="captured")
        ]
    if bad == "summarized_provider":
        exports[0]["intelligence_snapshot"] = {
            "provider_evidence": [{"status": "current"}]
        }
    if bad == "missing_identity":
        exports[0]["request"].update(merchant_id="mid_unknown", merchant_version=1)
    with pytest.raises(ValueError):
        curate(exports, labels, "2026-02-01T00:00:00+00:00")


def test_threshold_proposal_uses_calibration_only_and_rejects_leakage():
    calibration = [
        dict(case_id="old1", expected="BLOCK", risk_score=50),
        dict(case_id="old2", expected="APPROVE", risk_score=10),
    ]
    test = [dict(case_id="new1", expected="BLOCK", risk_score=1)]
    first = threshold_report(calibration, test)
    test[0]["risk_score"] = 99
    second = threshold_report(calibration, test)
    assert (
        first["proposal_threshold"] == second["proposal_threshold"]
        and not first["auto_applied"]
    )
    with pytest.raises(ValueError):
        threshold_report(calibration, calibration)
    with pytest.raises(ValueError):
        threshold_report(
            [dict(case_id="none", expected="APPROVE", risk_score=None)], test
        )


def test_isolated_replay_uses_observed_reservation_instead_of_reapproving_history(
    tmp_path,
):
    exports, labels = example()
    for row, label in zip(exports, labels):
        prior = deepcopy(row["request"])
        prior["request_id"] = "old_" + prior["request_id"]
        prior["requested_at_iso"] = prior["requested_at_iso"].replace("12:00", "11:00")
        prior["requested_at_hour"] = 11
        label["history_records"] = [
            dict(
                request=prior,
                verdict="HOLD",
                human_override="APPROVE",
                payment_state="unknown",
            )
        ]
        label["verdict"] = "BLOCK"
    cases = curate(exports, labels, "2026-02-01T00:00:00+00:00")["calibration"]
    dataset = tmp_path / "cases.json"
    dataset.write_text(json.dumps(cases))
    report = tmp_path / "report.json"
    env = dict(
        os.environ,
        PYTHONPATH=str(Path(__file__).resolve().parents[1]),
        OPENROUTER_API_KEY="",
        ANTHROPIC_API_KEY="",
    )
    r = subprocess.run(
        [
            sys.executable,
            "-m",
            "app.eval.eval_harness",
            str(dataset),
            "--output",
            str(report),
        ],
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert r.returncode == 0, r.stderr
    data = json.loads(report.read_text())
    assert data["predictions"][0]["actual"] == "BLOCK" and data["overall_accuracy"] == 1
    assert data["runtime"]["llm_mode"] == "mock"


def test_experiment_runs_both_time_splits_and_retrieval_ablation_in_isolated_processes(
    tmp_path,
):
    from app.eval.feedback_curation import canonical_hash

    exports, labels = example()
    splits = curate(exports, labels, "2026-02-01T00:00:00+00:00")
    for name, cases in splits.items():
        (tmp_path / (name + ".json")).write_text(json.dumps(cases))
    (tmp_path / "manifest.json").write_text(
        json.dumps(
            dict(
                cutoff="2026-02-01T00:00:00+00:00",
                hashes={k: canonical_hash(v) for k, v in splits.items()},
            )
        )
    )
    env = dict(
        os.environ,
        PYTHONPATH=str(Path(__file__).resolve().parents[1]),
        OPENROUTER_API_KEY="",
        ANTHROPIC_API_KEY="",
    )
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "app.eval.feedback_experiment",
            "--curated-dir",
            str(tmp_path),
            "--output-dir",
            str(tmp_path / "results"),
        ],
        env=env,
        capture_output=True,
        text=True,
        timeout=40,
    )
    assert result.returncode == 0, result.stderr
    report = json.loads((tmp_path / "results/experiment.json").read_text())
    assert set(report["ablation"]) == {"retrieval", "no_retrieval"}
    assert all(r["llm_mode"] == "mock" for r in report["runtime"].values())
    assert report["source_revision"]["commit"] and report["auto_applied"] is False
    assert (
        report["threshold_proposals"]["retrieval"]["selected_from"]
        == "calibration_only"
    )

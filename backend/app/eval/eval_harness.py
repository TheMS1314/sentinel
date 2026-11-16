"""
Sentinel Eval Harness
======================
Runs the golden dataset through the real orchestrator pipeline (rule
engine + LLM reasoner, exactly as production would) and scores the
output against each case's labeled expected_verdict.

Reports:
  - Overall accuracy
  - Per-class (APPROVE/HOLD/BLOCK) precision, recall, F1
  - Confusion matrix
  - Breakdown split by decided_by (rule_engine vs llm_reasoner) --
    this is the important one: rule_engine cases should score ~100%
    (deterministic), llm_reasoner cases show the reasoner's real
    performance, which is expected to be lower under mock mode and
    should improve once a live LLM key is configured.
  - A list of every miss, with full detail, for manual inspection.

Usage:
    python -m app.eval.eval_harness
"""

from __future__ import annotations
import json
import os
import sys
import uuid

from app.core.env import load_env

load_env()  # must run before any app import below reads os.environ
from datetime import datetime, timezone

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

from sklearn.metrics import precision_recall_fscore_support, confusion_matrix

# Never evaluate against application data or invoke a payment provider.
import tempfile

os.environ["SENTINEL_DATABASE_URL"] = "sqlite:///" + tempfile.mktemp(
    prefix="sentinel-eval-", suffix=".sqlite"
)
os.environ["RAZORPAY_KEY_ID"] = ""
os.environ["RAZORPAY_KEY_SECRET"] = ""
os.environ["SENTINEL_ENV"] = "evaluation"

from app.models.db import init_db, SessionLocal, engine
from app.models.policy import AgentPolicy, TransactionRequest, TimeWindow, VelocityLimit
from app.core.orchestrator import SentinelOrchestrator

VERDICT_LABELS = ["APPROVE", "HOLD", "BLOCK"]


def _policy_from_case(case: dict) -> tuple[AgentPolicy, str]:
    overrides = case["policy_overrides"]
    agent_id = (
        case["request"]["agent_id"] + "_" + uuid.uuid4().hex[:6]
    )  # unique per case, no cross-case history bleed
    base = dict(
        policy_id=f"pol_{case['case_id']}",
        agent_id=agent_id,
        owner_human_id="human_golden",
        max_amount_per_txn=2000.0,
        max_daily_total=5000.0,
        allowed_categories=["grocery"],
        time_window=TimeWindow(6, 22),
        velocity=VelocityLimit(3, 1440),
    )
    if "blocked_merchants" in overrides:
        base["blocked_merchants"] = overrides["blocked_merchants"]
    if "allowed_merchants" in overrides:
        base["allowed_merchants"] = overrides["allowed_merchants"]
    if "allowed_categories" in overrides:
        base["allowed_categories"] = overrides["allowed_categories"]
    if "velocity_max_count" in overrides or "velocity_window_minutes" in overrides:
        base["velocity"] = VelocityLimit(
            overrides.get("velocity_max_count", 3),
            overrides.get("velocity_window_minutes", 1440),
        )
    if "max_daily_total" in overrides:
        base["max_daily_total"] = overrides["max_daily_total"]
    if case.get("policy"):
        base.update(
            {
                k: v
                for k, v in case["policy"].items()
                if k not in ("policy_id", "agent_id")
            }
        )
        if isinstance(base.get("time_window"), dict):
            base["time_window"] = TimeWindow(**base["time_window"])
        if isinstance(base.get("velocity"), dict):
            base["velocity"] = VelocityLimit(**base["velocity"])
    return AgentPolicy(**base), agent_id


def _request_from_dict(
    d: dict, agent_id: str, request_id_suffix: str = ""
) -> TransactionRequest:
    d = dict(d)
    d["agent_id"] = agent_id
    d["request_id"] = d["request_id"] + request_id_suffix
    return TransactionRequest(**d)


def run_eval(dataset_path: str = None, verbose: bool = True) -> dict:
    if dataset_path is None:
        dataset_path = os.path.join(
            os.path.dirname(__file__), "..", "..", "data", "golden_dataset.json"
        )
        dataset_path = os.path.abspath(dataset_path)

    with open(dataset_path) as f:
        cases = json.load(f)

    if engine.url.get_backend_name() != "sqlite" or "sentinel-eval-" not in str(
        engine.url.database
    ):
        raise RuntimeError(
            "Run evaluations in a separate process against the dedicated evaluation database"
        )
    if os.environ.get("SENTINEL_EVAL_REQUIRE_LIVE") == "1":
        from app.core.llm_client import LLMClient

        if LLMClient().mock_mode:
            raise RuntimeError(
                "Live evaluation requested but provider credentials are absent"
            )
    init_db()
    db = SessionLocal()
    orchestrator = SentinelOrchestrator(db)

    y_true, y_pred, decided_by_list, misses = [], [], [], []

    for case in cases:
        from app.core.validation import validate_request, utc_timestamp

        for row in [case["request"], *case["history"]]:
            validate_request(TransactionRequest(**row))
        if any(
            utc_timestamp(h["requested_at_iso"])
            > utc_timestamp(case["request"]["requested_at_iso"])
            for h in case["history"]
        ):
            raise ValueError("Future history in " + case["case_id"])
        policy, agent_id = _policy_from_case(case)
        unique_suffix = "_" + uuid.uuid4().hex[:6]

        # Replay history first (if any) under the same unique agent_id,
        # so velocity/daily-total checks see it, exactly like production would.
        for h in case["history"]:
            h_req = _request_from_dict(h, agent_id, unique_suffix)
            orchestrator.evaluate_transaction(
                policy, h_req, scenario_tag="golden_history"
            )

        request = _request_from_dict(case["request"], agent_id, unique_suffix)
        decision = orchestrator.evaluate_transaction(
            policy, request, scenario_tag="golden_eval"
        )

        y_true.append(case["expected_verdict"])
        y_pred.append(decision.verdict)
        decided_by_list.append(decision.decided_by)

        if decision.verdict != case["expected_verdict"]:
            misses.append(
                {
                    "case_id": case["case_id"],
                    "category": case["category"],
                    "expected": case["expected_verdict"],
                    "actual": decision.verdict,
                    "decided_by": decision.decided_by,
                    "reasons": decision.reasons,
                    "notes": case["notes"],
                }
            )

    db.commit()

    precision, recall, f1, support = precision_recall_fscore_support(
        y_true, y_pred, labels=VERDICT_LABELS, zero_division=0
    )
    accuracy = sum(1 for t, p in zip(y_true, y_pred) if t == p) / len(y_true)
    cm = confusion_matrix(y_true, y_pred, labels=VERDICT_LABELS)

    def subset_accuracy(decider: str):
        pairs = [
            (t, p) for t, p, d in zip(y_true, y_pred, decided_by_list) if d == decider
        ]
        if not pairs:
            return None, 0
        acc = sum(1 for t, p in pairs if t == p) / len(pairs)
        return acc, len(pairs)

    rule_engine_acc, rule_engine_n = subset_accuracy("rule_engine")
    llm_acc, llm_n = subset_accuracy("llm_reasoner")

    report = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "llm_provider": orchestrator.reasoner.llm.provider,
        "runtime": {
            "llm_mode": "mock" if orchestrator.reasoner.llm.mock_mode else "live",
            "model": orchestrator.reasoner.llm.model,
            "prompt_version": __import__(
                "app.core.llm_reasoner", fromlist=["PROMPT_VERSION"]
            ).PROMPT_VERSION,
            "retrieval": os.environ.get("SENTINEL_DISABLE_RETRIEVAL") != "1",
            "dataset_sha256": __import__("hashlib")
            .sha256(open(dataset_path, "rb").read())
            .hexdigest(),
        },
        "total_cases": len(cases),
        "overall_accuracy": round(accuracy, 4),
        "per_class": {
            label: {
                "precision": round(float(p), 4),
                "recall": round(float(r), 4),
                "f1": round(float(f), 4),
                "support": int(s),
            }
            for label, p, r, f, s in zip(VERDICT_LABELS, precision, recall, f1, support)
        },
        "confusion_matrix": {
            "labels": VERDICT_LABELS,
            "matrix": cm.tolist(),
        },
        "by_decider": {
            "rule_engine": {
                "accuracy": round(rule_engine_acc, 4)
                if rule_engine_acc is not None
                else None,
                "n": rule_engine_n,
            },
            "llm_reasoner": {
                "accuracy": round(llm_acc, 4) if llm_acc is not None else None,
                "n": llm_n,
            },
        },
        "misses": misses,
    }

    report["safety"] = {
        "unsafe_approvals": sum(
            1 for t, p in zip(y_true, y_pred) if p == "APPROVE" and t != "APPROVE"
        ),
        "unnecessary_holds": sum(
            1 for t, p in zip(y_true, y_pred) if p == "HOLD" and t == "APPROVE"
        ),
        "hold_rate": sum(p == "HOLD" for p in y_pred) / len(y_pred),
    }
    db.close()
    if verbose:
        _print_report(report)

    return report


def _print_report(report: dict):
    print("=" * 60)
    print(f"SENTINEL GOLDEN DATASET EVAL -- {report['total_cases']} cases")
    print("=" * 60)
    print(f"\nOverall accuracy: {report['overall_accuracy'] * 100:.1f}%\n")

    print("Per-class metrics:")
    print(
        f"  {'Verdict':10} {'Precision':>10} {'Recall':>10} {'F1':>10} {'Support':>10}"
    )
    for label, m in report["per_class"].items():
        print(
            f"  {label:10} {m['precision']:>10.2f} {m['recall']:>10.2f} {m['f1']:>10.2f} {m['support']:>10}"
        )

    print("\nConfusion matrix (rows=expected, cols=actual):")
    labels = report["confusion_matrix"]["labels"]
    print(f"  {'':10}" + "".join(f"{l:>10}" for l in labels))
    for label, row in zip(labels, report["confusion_matrix"]["matrix"]):
        print(f"  {label:10}" + "".join(f"{v:>10}" for v in row))

    print("\nAccuracy by decision layer:")
    for decider, stats in report["by_decider"].items():
        if stats["n"] > 0:
            print(f"  {decider:15} {stats['accuracy'] * 100:.1f}%  (n={stats['n']})")
        else:
            print(f"  {decider:15} no cases routed here")

    if report["runtime"]["llm_mode"] == "mock":
        print(
            "\nMOCK evaluation: constructed examples and conservative canned reasoning; no live model accuracy claim."
        )
    else:
        print(
            f"\nLive provider: {report['llm_provider']}; model: {report['runtime']['model']}"
        )

    if report["misses"]:
        print(f"\n{len(report['misses'])} misses:")
        for m in report["misses"]:
            print(
                f"  [{m['category']}] {m['case_id']}: expected {m['expected']}, got {m['actual']} (via {m['decided_by']})"
            )
            print(f"    reason given: {m['reasons']}")
    else:
        print("\nNo misses.")


if __name__ == "__main__":
    import sys

    dataset_path = sys.argv[1] if len(sys.argv) > 1 else None
    report = run_eval(dataset_path)
    out_name = (
        (
            "prevalence_eval_report.json"
            if dataset_path and "prevalence" in dataset_path
            else "hard_eval_report.json"
        )
        if dataset_path
        else "eval_report.json"
    )
    out_path = os.path.join(os.path.dirname(__file__), "..", "..", "data", out_name)
    out_path = os.path.abspath(out_path)
    with open(out_path, "w") as f:
        json.dump(report, f, indent=2)
    print(f"\nFull report saved to {out_path}")

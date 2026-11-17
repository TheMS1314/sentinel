"""Explicit human labels and history review become versioned, time-separated cases.

This offline tool never trains a model, changes production policy or accepts a
review override as independent ground truth without a curator's decision.
"""

import argparse
from copy import deepcopy
from datetime import datetime, timezone
import hashlib
import json
import math
from pathlib import Path
from app.core.validation import utc_timestamp, validate_request, validate_policy
from app.models.policy import TransactionRequest, AgentPolicy

LABELS = {"APPROVE", "HOLD", "BLOCK"}
STATES = {
    "pending",
    "processing",
    "unknown",
    "created",
    "mock",
    "captured",
    "failed",
    "expired",
    "cancelled",
    "not_sent",
}


def canonical_hash(value):
    return hashlib.sha256(
        json.dumps(
            value, sort_keys=True, separators=(",", ":"), allow_nan=False
        ).encode()
    ).hexdigest()


def curate(exports, labels, cutoff):
    boundary = utc_timestamp(cutoff)
    source = {row["case_id"]: row for row in exports}
    if len(source) != len(exports):
        raise ValueError("Duplicate exported case IDs")
    splits = {"calibration": [], "test": []}
    seen = set()
    for label in labels:
        cid = label["case_id"]
        if cid in seen or cid not in source:
            raise ValueError("Duplicate or unknown label case")
        seen.add(cid)
        row = deepcopy(source[cid])
        if (
            label.get("verdict") not in LABELS
            or label.get("history_reviewed") is not True
            or label.get("context_reviewed") is not True
        ):
            raise ValueError("Explicit label, history and context review required")
        curator = label.get("curator", "").strip()
        if (
            not curator
            or curator == row.get("reviewer")
            or not label.get("rationale", "").strip()
        ):
            raise ValueError("An independent curator and rationale are required")
        created = utc_timestamp(row["source_created_at"])
        reviewed = utc_timestamp(label["reviewed_at"])
        if reviewed < created or reviewed > datetime.now(timezone.utc):
            raise ValueError("Invalid review chronology")
        policy = AgentPolicy.from_dict(row["policy"])
        validate_policy(policy)
        req = TransactionRequest(**row["request"])
        validate_request(req)
        history = label.get("history_records")
        if not isinstance(history, list) or len(history) > 200:
            raise ValueError(
                "Provide up to 200 explicit history records, including an explicit empty list"
            )
        ids = {req.request_id}
        for item in history:
            prior = TransactionRequest(**item["request"])
            validate_request(prior)
            if (
                prior.agent_id != req.agent_id
                or prior.request_id in ids
                or utc_timestamp(prior.requested_at_iso)
                > utc_timestamp(req.requested_at_iso)
            ):
                raise ValueError(
                    "History must be unique, same agent and never from the future"
                )
            ids.add(prior.request_id)
            if (
                item.get("verdict") not in LABELS
                or item.get("payment_state") not in STATES
                or item.get("human_override") not in (None, "APPROVE", "BLOCK")
            ):
                raise ValueError(
                    "History requires original decision and observed ledger state"
                )
            if (
                item["payment_state"]
                in {"pending", "processing", "unknown", "created", "mock", "captured"}
                and item["verdict"] != "APPROVE"
                and item.get("human_override") != "APPROVE"
            ):
                raise ValueError(
                    "Reserved/spent history requires an observed authorization"
                )
            if (
                item.get("budget_day")
                and utc_timestamp(item["budget_day"] + "T00:00:00+00:00")
                .date()
                .isoformat()
                != item["budget_day"]
            ):
                raise ValueError("Invalid history budget date")
        context = row.get("intelligence_snapshot") or {}
        # The scenario harness cannot recreate historic peer state or licensed
        # enrichment from only a summarized decision. Exclude instead of inventing it.
        if (
            context.get("provider_evidence")
            or context.get("vendor", {}).get("status") not in (None, "unknown")
            or context.get("threat", {}).get("matches")
            or context.get("threat", {}).get("expired_matches")
            or context.get("relations", {}).get("alert")
        ):
            raise ValueError(
                "This export needs a separately reconstructed enrichment/peer scenario; summarized evidence is insufficient for replay"
            )
        if req.merchant_id:
            identity = row.get("identity_snapshot")
            if (
                not identity
                or identity.get("merchant_id") != req.merchant_id
                or identity.get("version") != req.merchant_version
            ):
                raise ValueError("A matching immutable identity snapshot is required")
        row.update(
            expected_verdict=label["verdict"],
            history=[],
            history_records=history,
            requires_history_review=False,
            is_judgment_call=True,
            policy_overrides={},
            notes=label["rationale"],
        )
        row["curation"] = dict(
            curator=curator,
            reviewed_at=reviewed.isoformat(),
            source_case_hash=canonical_hash(source[cid]),
            label_hash=canonical_hash(label),
            method="independent-human-label-v1",
            context_scope="single-agent policy/ledger/operator identity; no summarized commercial/peer replay",
        )
        splits["calibration" if created < boundary else "test"].append(row)
    if not all(splits.values()):
        raise ValueError("Both time-separated splits need reviewed cases")
    for rows in splits.values():
        rows.sort(key=lambda r: (r["source_created_at"], r["case_id"]))
    return splits


def threshold_report(calibration, test):
    """A proposal only: risk score is not a probability and never changes policy."""

    def checked(rows):
        seen = set()
        for r in rows:
            score = r.get("risk_score")
            if r.get("expected") not in LABELS or r["case_id"] in seen:
                raise ValueError("Invalid or duplicate evaluation record")
            seen.add(r["case_id"])
            if score is not None and (
                isinstance(score, bool)
                or not isinstance(score, (int, float))
                or not math.isfinite(score)
                or not 0 <= score <= 100
            ):
                raise ValueError("Invalid risk score")
        return seen

    if checked(calibration) & checked(test):
        raise ValueError("Calibration/test case leakage")

    def metrics(rows, threshold):
        usable = [r for r in rows if r.get("risk_score") is not None]
        unsafe = sum(
            r["risk_score"] < threshold and r["expected"] != "APPROVE" for r in usable
        )
        holds = sum(
            r["risk_score"] >= threshold and r["expected"] == "APPROVE" for r in usable
        )
        return dict(
            scored=len(usable),
            excluded_no_score=len(rows) - len(usable),
            unsafe_approvals=unsafe,
            unnecessary_holds=holds,
            weighted_cost=10 * unsafe + holds,
        )

    if not any(r.get("risk_score") is not None for r in calibration) or not any(
        r.get("risk_score") is not None for r in test
    ):
        raise ValueError(
            "Both splits need actual scored cases; missing scores are not zero"
        )
    sweep = [dict(threshold=t, **metrics(calibration, t)) for t in range(0, 101, 5)]
    best = min(sweep, key=lambda r: (r["weighted_cost"], r["threshold"]))
    return dict(
        method="time-split-risk-threshold-proposal-v1",
        selected_from="calibration_only",
        proposal_threshold=best["threshold"],
        calibration_sweep=sweep,
        test=metrics(test, best["threshold"]),
        auto_applied=False,
        limitations="Uncalibrated composite score; 10:1 illustrative costs; small/nonrepresentative labels cannot prove fraud performance; no confidence/probability claim",
        calibration_hash=canonical_hash(calibration),
        test_hash=canonical_hash(test),
    )


def validate_reports(calibration, test):
    for key in ("llm_mode", "model", "prompt_version", "retrieval"):
        if calibration["runtime"].get(key) != test["runtime"].get(key):
            raise ValueError(
                "Split reports must use matching runtime, model, prompt and retrieval settings"
            )
    if calibration["runtime"]["dataset_sha256"] == test["runtime"]["dataset_sha256"]:
        raise ValueError("Calibration and test datasets must differ")
    for report in (calibration, test):
        if not report.get("predictions") or any(
            not r.get("curation") or not r.get("source_created_at")
            for r in report["predictions"]
        ):
            raise ValueError("Independent curation and source event time are required")
    if max(
        utc_timestamp(r["source_created_at"]) for r in calibration["predictions"]
    ) >= min(utc_timestamp(r["source_created_at"]) for r in test["predictions"]):
        raise ValueError("Calibration events must precede all held-out test events")


def write_private(path, value):
    from app.core.private_artifacts import write_private_json

    write_private_json(path, value)


def main():
    p = argparse.ArgumentParser()
    sub = p.add_subparsers(dest="command", required=True)
    c = sub.add_parser("curate")
    c.add_argument("--exports", required=True)
    c.add_argument("--labels", required=True)
    c.add_argument("--cutoff", required=True)
    c.add_argument("--output-dir", required=True)
    r = sub.add_parser("report")
    r.add_argument("--calibration", required=True)
    r.add_argument("--test", required=True)
    r.add_argument("--output", required=True)
    a = p.parse_args()
    if a.command == "curate":
        splits = curate(
            json.loads(Path(a.exports).read_text()),
            json.loads(Path(a.labels).read_text()),
            a.cutoff,
        )
        for name, rows in splits.items():
            write_private(Path(a.output_dir) / (name + ".json"), rows)
        write_private(
            Path(a.output_dir) / "manifest.json",
            dict(
                cutoff=a.cutoff,
                counts={k: len(v) for k, v in splits.items()},
                hashes={k: canonical_hash(v) for k, v in splits.items()},
                method="independent-human-label-v1",
            ),
        )
    else:
        cal = json.loads(Path(a.calibration).read_text())
        test = json.loads(Path(a.test).read_text())
        validate_reports(cal, test)
        write_private(
            a.output, threshold_report(cal["predictions"], test["predictions"])
        )
    print("Offline artifact written; no policy, authority or model was changed")


if __name__ == "__main__":
    main()

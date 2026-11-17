"""Reproducible, isolated retrieval ablation on independent chronological splits."""

import argparse
import json
import os
from pathlib import Path
from app.ops.provenance import revision
import subprocess
import sys
from app.eval.feedback_curation import (
    validate_reports,
    canonical_hash,
    threshold_report,
    write_private,
)


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--curated-dir", required=True)
    p.add_argument("--output-dir", required=True)
    p.add_argument("--require-live", action="store_true")
    a = p.parse_args()
    source = Path(a.curated_dir)
    output = Path(a.output_dir)
    output.mkdir(parents=True, exist_ok=True)
    manifest = json.loads((source / "manifest.json").read_text())
    for split in ("calibration", "test"):
        cases = json.loads((source / (split + ".json")).read_text())
        if canonical_hash(cases) != manifest["hashes"][split]:
            raise ValueError("Curated dataset hash mismatch")
        if any(
            c.get("requires_history_review") or not c.get("curation") for c in cases
        ):
            raise ValueError("Explicit curation required")
    reports = {}
    for retrieval in (True, False):
        mode = "retrieval" if retrieval else "no_retrieval"
        reports[mode] = {}
        for split in ("calibration", "test"):
            destination = output / (mode + "_" + split + ".json")
            env = dict(
                os.environ,
                SENTINEL_DISABLE_RETRIEVAL="0" if retrieval else "1",
                SENTINEL_EVAL_REQUIRE_LIVE="1" if a.require_live else "0",
            )
            process = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "app.eval.eval_harness",
                    str((source / (split + ".json")).resolve()),
                    "--output",
                    str(destination.resolve()),
                ],
                env=env,
                capture_output=True,
                timeout=600,
            )
            if process.returncode:
                raise RuntimeError(
                    "Isolated evaluation failed; no partial result is a successful experiment"
                )
            reports[mode][split] = json.loads(destination.read_text())
            destination.chmod(0o600)
    for rows in reports.values():
        validate_reports(rows["calibration"], rows["test"])
    proposals = {
        mode: threshold_report(
            rows["calibration"]["predictions"], rows["test"]["predictions"]
        )
        for mode, rows in reports.items()
    }
    write_private(
        output / "experiment.json",
        dict(
            source_manifest=manifest,
            source_revision=revision(),
            runtime={mode: rows["test"]["runtime"] for mode, rows in reports.items()},
            ablation={
                mode: {
                    split: dict(accuracy=r["overall_accuracy"], **r["safety"])
                    for split, r in rows.items()
                }
                for mode, rows in reports.items()
            },
            threshold_proposals=proposals,
            auto_applied=False,
        ),
    )
    print(
        "Offline experiment complete; inspect mode, provenance, split sizes and limitations before drawing conclusions"
    )


if __name__ == "__main__":
    main()

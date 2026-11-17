"""Enforce npm audit with one explicit expiring build-only exception.

This exception is not a vulnerability fix. Runtime findings never inherit it.
"""

from datetime import date, datetime, timezone
import json
from pathlib import Path
import subprocess

ROOT = Path(__file__).resolve().parents[1]
ADVISORY = "https://github.com/advisories/GHSA-fc5q-39xc-vjpf"
EXPIRES = date(2026, 12, 1)


def assess(full, runtime, lock, today=None, latest_picomatch="4.0.7"):
    today = today or datetime.now(timezone.utc).date()
    for report in (full, runtime):
        if (
            not isinstance(report, dict)
            or report.get("error")
            or report.get("auditReportVersion") != 2
            or not isinstance(report.get("vulnerabilities"), dict)
        ):
            raise ValueError(
                "Unusable npm audit report; verification cannot be skipped"
            )
    if runtime["vulnerabilities"]:
        raise ValueError("Runtime dependency findings are never excepted")
    entries = full["vulnerabilities"]
    if not entries:
        return dict(runtime_findings=0, development_findings=0, exception=None)
    if today >= EXPIRES:
        raise ValueError(
            "Development advisory exception expired; dependency review required"
        )
    if latest_picomatch != "4.0.7":
        raise ValueError("New upstream picomatch release requires dependency review")
    if not isinstance(lock.get("packages"), dict):
        raise ValueError("Lockfile package scopes are required")

    def verify(name, seen):
        if name in seen or name not in entries:
            raise ValueError("Unresolved advisory dependency chain")
        entry = entries[name]
        nodes = entry.get("nodes")
        if not nodes or any(
            lock["packages"].get(node, {}).get("dev") is not True for node in nodes
        ):
            raise ValueError(
                "Exception requires every affected package to be development-only"
            )
        if not entry.get("via"):
            raise ValueError("Finding has no verifiable advisory provenance")
        for cause in entry["via"]:
            if isinstance(cause, str):
                verify(cause, seen | {name})
            elif (
                not isinstance(cause, dict)
                or cause.get("name") != "picomatch"
                or cause.get("url") != ADVISORY
                or not allowed_parent_recommendation(entry.get("fixAvailable"))
            ):
                raise ValueError(
                    "New advisory or available fix requires dependency review"
                )

    for name in entries:
        verify(name, set())
    return dict(
        runtime_findings=0,
        development_findings=len(entries),
        exception=ADVISORY,
        expires=EXPIRES.isoformat(),
        disposition="unpatched build/test dependency; fixed project globs, loopback-only dev servers, no Node server in deployed frontend",
    )


def allowed_parent_recommendation(fix):
    # npm can propose incompatible changes to a parent build tool even though
    # the advisory has no picomatch patch. Never run audit fix --force. The
    # independent upstream-version check and expiry still require review.
    return fix is False or (
        isinstance(fix, dict)
        and fix.get("name") in ("vite", "vitest", "tailwindcss")
        and fix.get("isSemVerMajor") is True
    )


def audit(*options):
    result = subprocess.run(
        ["npm", "audit", "--json", *options],
        cwd=ROOT / "frontend",
        capture_output=True,
        timeout=120,
    )
    if result.returncode not in (0, 1):
        raise RuntimeError("npm audit transport/command failed")
    return json.loads(result.stdout)


def main():
    full, runtime = audit(), audit("--omit=dev")
    output = ROOT / "artifacts/frontend-audit.json"
    output.parent.mkdir(exist_ok=True)
    report = dict(full=full, runtime=runtime)
    try:
        latest = subprocess.run(
            ["npm", "view", "picomatch", "version", "--json"],
            cwd=ROOT / "frontend",
            capture_output=True,
            timeout=60,
            check=True,
        )
        report["latest_picomatch"] = json.loads(latest.stdout)
        summary = assess(
            full,
            runtime,
            json.loads((ROOT / "frontend/package-lock.json").read_text()),
            latest_picomatch=report["latest_picomatch"],
        )
        report["summary"] = summary
    except ValueError as exc:
        report["policy_error"] = str(exc)
        raise
    finally:
        output.write_text(json.dumps(report, indent=2) + "\n")
    if summary["exception"]:
        print(
            "::warning title=Unpatched build dependency::GHSA-fc5q-39xc-vjpf remains in development tooling only; exception expires "
            + summary["expires"]
        )
    print("FRONTEND_AUDIT_POLICY_PASS " + json.dumps(summary), flush=True)


if __name__ == "__main__":
    main()

"""A scoped dev-tool exception must never accept unrelated or runtime findings."""

from datetime import date
import copy
import importlib.util
from pathlib import Path
import pytest

spec = importlib.util.spec_from_file_location(
    "frontend_audit",
    Path(__file__).resolve().parents[2] / "scripts/check_frontend_audit.py",
)
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


def fixture():
    full = dict(
        auditReportVersion=2,
        vulnerabilities={
            "picomatch": dict(
                nodes=["node_modules/picomatch"],
                fixAvailable=False,
                via=[dict(name="picomatch", url=module.ADVISORY)],
            ),
            "vite": dict(nodes=["node_modules/vite"], via=["picomatch"]),
        },
    )
    runtime = dict(auditReportVersion=2, vulnerabilities={})
    lock = dict(
        packages={
            "node_modules/picomatch": dict(dev=True),
            "node_modules/vite": dict(dev=True),
        }
    )
    return full, runtime, lock


def test_known_build_only_advisory_is_visible_and_expires():
    full, runtime, lock = fixture()
    summary = module.assess(full, runtime, lock, date(2026, 11, 17))
    assert summary["runtime_findings"] == 0 and summary["development_findings"] == 2
    assert summary["exception"] == module.ADVISORY
    with pytest.raises(ValueError, match="expired"):
        module.assess(full, runtime, lock, date(2026, 12, 1))


@pytest.mark.parametrize(
    "mutation",
    [
        "runtime",
        "scope",
        "new_advisory",
        "patch",
        "missing_chain",
        "cycle",
        "error",
        "empty_provenance",
    ],
)
def test_other_audit_conditions_fail_closed(mutation):
    full, runtime, lock = fixture()
    if mutation == "runtime":
        runtime["vulnerabilities"] = copy.deepcopy(full["vulnerabilities"])
    elif mutation == "scope":
        lock["packages"]["node_modules/picomatch"]["dev"] = False
    elif mutation == "new_advisory":
        full["vulnerabilities"]["picomatch"]["via"][0]["url"] = (
            "https://github.com/advisories/GHSA-other"
        )
    elif mutation == "patch":
        full["vulnerabilities"]["picomatch"]["fixAvailable"] = True
    elif mutation == "missing_chain":
        full["vulnerabilities"]["vite"]["via"] = ["unknown"]
    elif mutation == "cycle":
        full["vulnerabilities"]["picomatch"]["via"] = ["vite"]
    elif mutation == "error":
        runtime["error"] = dict(message="registry unavailable")
    elif mutation == "empty_provenance":
        full["vulnerabilities"]["picomatch"]["via"] = []
    with pytest.raises(ValueError):
        module.assess(full, runtime, lock, date(2026, 11, 17))


def test_fix_removal_needs_no_exception_after_expiry():
    full, runtime, lock = fixture()
    full["vulnerabilities"] = {}
    assert module.assess(full, runtime, lock, date(2027, 1, 1))["exception"] is None


def test_new_upstream_release_requires_review_even_before_expiry():
    full, runtime, lock = fixture()
    with pytest.raises(ValueError, match="upstream"):
        module.assess(full, runtime, lock, date(2026, 11, 17), latest_picomatch="4.0.8")


def test_known_npm_major_parent_rollback_is_not_a_picomatch_patch():
    full, runtime, lock = fixture()
    full["vulnerabilities"]["picomatch"]["fixAvailable"] = dict(
        name="vite", version="6.2.7", isSemVerMajor=True
    )
    assert module.assess(full, runtime, lock, date(2026, 11, 17))["exception"]
    full["vulnerabilities"]["picomatch"]["fixAvailable"] = dict(
        name="picomatch", version="4.0.8", isSemVerMajor=False
    )
    with pytest.raises(ValueError):
        module.assess(full, runtime, lock, date(2026, 11, 17))

"""
Tier 2: Hard/Adversarial Golden Dataset
==========================================
The original golden_dataset.py (89 cases) and prevalence_dataset.py
(500 cases) both score 100% -- which is a real methodological concern,
not just a nice number: it could mean the system is excellent, or it
could mean those datasets aren't hard enough to discriminate between
"works" and "works well." Both were largely solvable by the
DETERMINISTIC rule engine, which is inherently easy to get to 100% on
since it's just exact threshold comparisons.

This tier is deliberately different: every case here targets either
(a) the softer LLM-reasoning layer, (b) the merchant-matching edge
cases just fixed, or (c) known, documented limitations of the
behavioral baseline -- specifically so it CANNOT be trivially solved
by rule-matching alone.

Categories:
  merchant_near_miss     : case/whitespace variants of a blocked name
                            (should now BLOCK, post-fix) and genuinely
                            different names that share a root (should
                            NOT block -- a documented limitation)
  semantic_drift_no_kw    : intent that is ambiguous/borderline WITHOUT
                            any injection keyword and WITHOUT strong
                            keyword overlap either
  compound_weak_signals   : amount/timing/merchant signals that are
                            each individually unremarkable but combine
  gradual_drift_tolerance : a plausible, gradually-shifting "new
                            normal" -- tests whether the behavioral
                            baseline over-flags legitimate evolution

This dataset does NOT claim ground-truth verdicts with the same
confidence as the original golden set -- some cases are genuinely
judgment calls where reasonable systems could disagree.
"""

from __future__ import annotations
import json
import os
from dataclasses import dataclass, field, asdict


@dataclass
class HardCase:
    case_id: str
    category: str
    policy_overrides: dict
    request: dict
    history: list = field(default_factory=list)
    expected_verdict: str = "HOLD"
    is_judgment_call: bool = False
    notes: str = ""


BASE_REQUEST = dict(
    agent_id="agent_hard",
    merchant_name="FreshMart",
    merchant_category="grocery",
    amount=500.0,
    requested_at_hour=14,
    requested_at_iso="2026-08-24T14:00:00",
    stated_intent="buy weekly groceries",
    raw_agent_goal="buy weekly groceries under budget",
)


def _req(i: int, **overrides) -> dict:
    r = dict(BASE_REQUEST)
    r["request_id"] = f"hard_req_{i:03d}"
    r.update(overrides)
    from datetime import datetime
    if 'requested_at_iso' in overrides and 'requested_at_hour' not in overrides:
        r['requested_at_hour'] = datetime.fromisoformat(r['requested_at_iso']).hour
    else:
        r['requested_at_iso'] = datetime.fromisoformat(r['requested_at_iso']).replace(hour=r['requested_at_hour']).isoformat()
    return r


def build_dataset() -> list:
    cases = []
    i = 0

    blocked_name = "ShadyStore"
    should_still_block = ["shadystore", "SHADYSTORE", "  ShadyStore  ", "ShAdYsToRe"]
    for variant in should_still_block:
        i += 1
        cases.append(HardCase(
            case_id=f"hard_{i:03d}", category="merchant_near_miss",
            policy_overrides={"blocked_merchants": [blocked_name]},
            request=_req(i, merchant_name=variant),
            expected_verdict="BLOCK",
            notes=f"Case/whitespace variant of blocked merchant '{blocked_name}' -- must still be caught after normalization fix.",
        ))

    should_not_block = ["ShadyStore2", "ShadyStoreOutlet", "NotShadyStore"]
    for variant in should_not_block:
        i += 1
        cases.append(HardCase(
            case_id=f"hard_{i:03d}", category="merchant_near_miss",
            policy_overrides={"blocked_merchants": [blocked_name]},
            request=_req(i, merchant_name=variant),
            expected_verdict="APPROVE",
            notes=f"Genuinely different name from '{blocked_name}' -- correctly NOT blocked; catching this needs fuzzy matching, out of scope (false-positive risk).",
        ))

    semantic_cases = [
        ("pick up something nice for the team", "grocery", False),
        ("get supplies for tonight", "grocery", False),
        ("grab what's needed for the week", "grocery", True),
        ("cover this month's essentials", "grocery", False),
        ("take care of the usual order", "grocery", True),
    ]
    for intent, category, leans_ok in semantic_cases:
        i += 1
        cases.append(HardCase(
            case_id=f"hard_{i:03d}", category="semantic_drift_no_kw",
            policy_overrides={},
            request=_req(i, stated_intent=intent, merchant_category=category),
            expected_verdict="APPROVE" if leans_ok else "HOLD",
            is_judgment_call=True,
            notes="Genuinely ambiguous intent with no injection language and no strong keyword overlap -- "
                  "a judgment call requiring real semantic understanding, not string matching.",
        ))

    for j in range(3):
        i += 1
        history = [
            _req(i, request_id=f"hard_hist_{i:03d}_{k}", amount=195.0 + k * 4,
                 requested_at_hour=10, merchant_name="FreshMart",
                 requested_at_iso=f"2026-08-24T{9+k:02d}:00:00")
            for k in range(6)
        ]
        cases.append(HardCase(
            case_id=f"hard_{i:03d}", category="compound_weak_signals",
            policy_overrides={"velocity_max_count": 100},
            request=_req(i, amount=210.0 + j * 2, requested_at_hour=16 + j, merchant_name="NewCornerShop"),
            history=history,
            expected_verdict="HOLD",
            notes="No single signal (amount/timing/merchant) is extreme alone -- tests whether the "
                  "composite risk scorer catches the combination.",
        ))

    history = []
    for k in range(10):
        history.append(_req(
            9000 + k, request_id=f"hard_drift_hist_{k}",
            amount=200.0 + k * 20,
            requested_at_hour=14,
            requested_at_iso=f"2026-08-{20+k:02d}T14:00:00",
        ))
    i += 1
    cases.append(HardCase(
        case_id=f"hard_{i:03d}", category="gradual_drift_tolerance",
        # velocity_window_minutes=60 means only transactions within the last
        # HOUR count toward velocity -- realistic for "one purchase per day
        # over 10 days" history, which should never trip a same-day burst
        # check. max_daily_total raised so the daily-cap check (which is
        # legitimately a same-day concept) doesn't interfere either -- this
        # case is specifically about the BEHAVIORAL baseline, not the other
        # two checks, so both are deliberately loosened to isolate it.
        policy_overrides={"velocity_max_count": 50, "velocity_window_minutes": 60, "max_daily_total": 50000.0},
        request=_req(i, amount=400.0, requested_at_hour=14, requested_at_iso="2026-08-30T14:00:00"),
        history=history,
        expected_verdict="APPROVE",
        is_judgment_call=True,
        notes="A gradually-established new normal (200 -> 380 over 10 approved transactions, one per day) -- a real "
              "system should recognize 400 as a continuation of the trend. Honest stress test of a known "
              "limitation (the baseline weighs all approved history equally, not recency-weighted) -- "
              "expected to potentially fail, which is the point.",
    ))

    return cases


def save_dataset(path: str = None):
    if path is None:
        path = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "data", "hard_dataset.json"))
    cases = build_dataset()
    with open(path, "w") as f:
        json.dump([asdict(c) for c in cases], f, indent=2)
    return path, len(cases)


if __name__ == "__main__":
    path, count = save_dataset()
    print(f"Wrote {count} hard cases to {path}")
    from collections import Counter
    cases = build_dataset()
    by_category = Counter(c.category for c in cases)
    judgment_calls = sum(1 for c in cases if c.is_judgment_call)
    print(f"\nJudgment calls (genuinely ambiguous, not hard facts): {judgment_calls}/{count}")
    print("\nBy category:")
    for cat, n in sorted(by_category.items()):
        print(f"  {cat:28} {n}")

"""Explicit opt-in contract probes. Mock/missing credentials never count as verified."""

import argparse
from datetime import datetime, timezone
import hashlib
import json
import logging
import os
from pathlib import Path
from app.ops.provenance import revision
import uuid
from app.core.env import load_env

TARGETS = ("model", "razorpay_test", "recorded_future", "riskrecon")


def probe(target, execute=False):
    if target not in TARGETS:
        raise ValueError("Unsupported integration target")
    result = {
        "target": target,
        "status": "blocked",
        "mode": "unverified",
        "reason": None,
    }
    if not execute:
        return dict(
            result, reason="Explicit --execute opt-in required; no request sent"
        )
    try:
        if target == "model":
            from app.core.llm_client import LLMClient

            llm = LLMClient()
            if llm.mock_mode or not os.environ.get("LLM_MODEL"):
                return dict(
                    result, reason="Explicit model and provider credential required"
                )
            response = llm.complete_json(
                "Return JSON only: verdict HOLD, reasoning contract probe, confidence 0.5. Do not approve or invoke any tool.",
                "Synthetic integration schema check; no customer data.",
                {},
            )
            if (
                response.get("_usage", {}).get("mode") != "live"
                or response.get("verdict") != "HOLD"
            ):
                return dict(
                    result,
                    status="failed",
                    reason="Live structured-output contract failed",
                )
            return dict(
                result,
                status="verified",
                mode="live",
                model=llm.model,
                provider=llm.provider,
                usage=response["_usage"],
            )
        if target == "razorpay_test":
            key, secret = (
                os.environ.get("RAZORPAY_KEY_ID", ""),
                os.environ.get("RAZORPAY_KEY_SECRET", ""),
            )
            if not key or not secret:
                return dict(result, reason="Razorpay TEST credentials missing")
            if not key.startswith("rzp_test_"):
                return dict(
                    result, status="failed", reason="Live payment keys are prohibited"
                )
            from app.core.razorpay_gateway import RazorpayGateway

            gateway = RazorpayGateway()
            rid = "contract_" + uuid.uuid4().hex
            order = gateway.create_order(
                1, rid, {"purpose": "synthetic-test-order-no-capture"}
            )
            if order.get("status") != "created":
                return dict(
                    result,
                    status="failed",
                    reason="Test-order result is not known; inspect provider sandbox before retrying",
                )
            checked = gateway.reconcile(order["order_id"], rid, 100)
            if checked["status"] != "created":
                return dict(
                    result, status="failed", reason="Unexpected test-order state"
                )
            return dict(
                result,
                status="verified",
                mode="test_order_only",
                order_reference_hash=hashlib.sha256(
                    order["order_id"].encode()
                ).hexdigest(),
                amount_paise=100,
                capture_tested=False,
                webhook_tested=False,
            )
        from app.core.providers import fetch

        entity = os.environ.get(
            "SENTINEL_PROBE_DOMAIN_ID"
            if target == "recorded_future"
            else "SENTINEL_PROBE_RISKRECON_TOE",
            "",
        )
        if not entity:
            return dict(
                result, reason="An authorized provider probe entity is required"
            )
        response = fetch(target, entity)
        if response["status"] == "not_configured":
            return dict(result, reason=response["error"])
        if response["status"] != "current":
            return dict(
                result,
                status="failed",
                reason="Provider schema/freshness/transport contract failed",
                failure_type=response["error"],
            )
        return dict(
            result,
            status="verified",
            mode="licensed_read_only",
            response_hash=response["response_hash"],
            expires_at=response["expires_at"].isoformat(),
        )
    except Exception as exc:
        return dict(
            result,
            status="failed",
            reason="Integration probe failed",
            failure_type=type(exc).__name__,
        )


def main():
    load_env()
    logging.disable(
        logging.CRITICAL
    )  # Reports retain types, never SDK response/credential text.
    p = argparse.ArgumentParser()
    p.add_argument(
        "--execute",
        action="store_true",
        help="May spend bounded model tokens/create one INR1 test order; never captures or uses live keys",
    )
    p.add_argument("--targets", default=",".join(TARGETS))
    p.add_argument("--output", required=True)
    args = p.parse_args()
    targets = args.targets.split(",")
    if any(t not in TARGETS for t in targets) or len(targets) != len(set(targets)):
        p.error("Provide unique supported targets")
    results = [probe(t, args.execute) for t in targets]
    report = dict(
        generated_at=datetime.now(timezone.utc).isoformat(),
        source_revision=revision(),
        results=results,
        scope="transport/schema contracts only; not accuracy, settlement or launch approval",
    )
    path = Path(args.output)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(report, indent=2) + "\n")
    path.chmod(0o600)
    print(json.dumps({r["target"]: r["status"] for r in results}))
    return 0 if all(r["status"] == "verified" for r in results) else 2


if __name__ == "__main__":
    raise SystemExit(main())

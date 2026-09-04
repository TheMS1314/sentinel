"""
Razorpay Gateway
================
Thin wrapper around Razorpay's test-mode API. Only APPROVED decisions
ever reach this module -- HOLD and BLOCK verdicts stop before this point,
by design. This file has exactly one job: take an approved transaction
and actually attempt it against Razorpay's test-mode order API.

Falls back to a clearly-labeled mock mode if no API keys are configured,
so the rest of the system (and the demo) still runs end-to-end without
requiring live credentials -- but logs loudly that it's in mock mode so
nobody mistakes mock behavior for a real integration.
"""

from __future__ import annotations
import os
import logging

logger = logging.getLogger("sentinel.razorpay_gateway")

try:
    import razorpay
except ImportError:
    razorpay = None


class RazorpayGateway:
    def __init__(self):
        key_id = os.environ.get("RAZORPAY_KEY_ID", "")
        key_secret = os.environ.get("RAZORPAY_KEY_SECRET", "")
        self.mock_mode = not (key_id and key_secret and razorpay)
        if self.mock_mode:
            logger.warning(
                "RazorpayGateway running in MOCK MODE -- no API keys configured. "
                "Set RAZORPAY_KEY_ID / RAZORPAY_KEY_SECRET in backend/.env for "
                "real test-mode integration."
            )
            self.client = None
        else:
            self.client = razorpay.Client(auth=(key_id, key_secret))

    def create_order(self, amount_inr: float, request_id: str, notes: dict) -> dict:
        """
        Creates a Razorpay order in test mode for an APPROVED transaction.
        Amount is converted to paise (Razorpay's base unit) as required by the API.

        Returns a dict with at least: {"status": "created"|"mock"|"failed", "order_id": str|None}
        """
        amount_paise = int(round(amount_inr * 100))

        if self.mock_mode:
            fake_order_id = f"mock_order_{request_id}"
            logger.info(f"[MOCK] Would create Razorpay order for ₹{amount_inr:.2f} ({fake_order_id})")
            return {"status": "mock", "order_id": fake_order_id, "amount_paise": amount_paise}

        try:
            order = self.client.order.create(
                {
                    "amount": amount_paise,
                    "currency": "INR",
                    "receipt": request_id,
                    "notes": notes,
                }
            )
            return {"status": "created", "order_id": order.get("id"), "amount_paise": amount_paise}
        except Exception as e:
            logger.error(f"Razorpay order creation failed for {request_id}: {e}")
            return {"status": "failed", "order_id": None, "error": str(e)}

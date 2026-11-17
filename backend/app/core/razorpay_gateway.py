"""Test-order integration. Live keys are rejected; ambiguous outcomes need reconciliation."""

import os
import logging
from app.core.validation import paise

logger = logging.getLogger("sentinel.razorpay_gateway")
try:
    import razorpay
except ImportError:
    razorpay = None


class RazorpayGateway:
    def __init__(self):
        key = os.environ.get("RAZORPAY_KEY_ID", "")
        secret = os.environ.get("RAZORPAY_KEY_SECRET", "")
        if key and not key.startswith("rzp_test_"):
            raise RuntimeError("Only Razorpay TEST credentials are supported")
        self.mock_mode = not (key and secret and razorpay)
        if self.mock_mode and os.environ.get("SENTINEL_ENV") == "production":
            raise RuntimeError(
                "Production-profile startup requires explicit Razorpay test credentials"
            )
        self.client = None if self.mock_mode else razorpay.Client(auth=(key, secret))

    def create_order(self, amount_inr, request_id, notes):
        cents = paise(amount_inr)
        if self.mock_mode:
            return {
                "status": "mock",
                "order_id": f"mock_order_{request_id}",
                "amount_paise": cents,
            }
        # Razorpay receipt max length is 40. Stable digest retains idempotent correlation.
        import hashlib

        receipt = hashlib.sha256(request_id.encode()).hexdigest()[:40]
        try:
            order = self.client.order.create(
                {
                    "amount": cents,
                    "currency": "INR",
                    "receipt": receipt,
                    "notes": dict(notes, sentinel_request_id=request_id),
                },
                timeout=(3.05, 10),
            )
            if not order or not order.get("id"):
                return {
                    "status": "unknown",
                    "error": "Provider returned no order reference",
                }
            return {"status": "created", "order_id": order["id"], "amount_paise": cents}
        except Exception:
            # A timeout is not proof that an external side effect did not happen.
            logger.exception("Gateway order outcome unknown; reconciliation required")
            return {
                "status": "unknown",
                "order_id": None,
                "error": "Reconciliation required",
            }
        finally:
            self.client.session.close()

    def reconcile(self, order_id, request_id, amount_paise, merchant_id=""):
        if self.mock_mode:
            raise ValueError("Provider reconciliation requires test credentials")
        try:
            order = self.client.order.fetch(order_id, timeout=(3.05, 10))
        finally:
            self.client.session.close()
        if (
            order.get("amount") != amount_paise
            or order.get("currency") != "INR"
            or order.get("notes", {}).get("sentinel_request_id") != request_id
            or (
                merchant_id
                and order.get("notes", {}).get("sentinel_merchant_id") != merchant_id
            )
        ):
            raise ValueError("Order does not match the reserved request")
        return {
            "order_id": order_id,
            "status": "captured" if order.get("status") == "paid" else "created",
        }

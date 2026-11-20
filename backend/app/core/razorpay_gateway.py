"""Test-order integration. Live keys are rejected; ambiguous outcomes need reconciliation."""

import os
import logging
import hashlib
import re
from app.core.validation import paise

logger = logging.getLogger("sentinel.razorpay_gateway")


def valid_reference(value):
    return (
        isinstance(value, str)
        and re.fullmatch(r"order_[A-Za-z0-9]{1,122}", value) is not None
    )


def validate_order(order, request_id, amount_paise, *, order_id=None, merchant_id=""):
    """Provider output must agree with the durable request, never supply authority."""
    if (
        not isinstance(order, dict)
        or not valid_reference(order.get("id"))
        or (order_id is not None and order["id"] != order_id)
        or type(order.get("amount")) is not int
        or order["amount"] != amount_paise
        or order.get("currency") != "INR"
        or order.get("status") not in ("created", "attempted", "paid")
        or not isinstance(order.get("notes"), dict)
        or order["notes"].get("sentinel_request_id") != request_id
        or (merchant_id and order["notes"].get("sentinel_merchant_id") != merchant_id)
    ):
        raise ValueError(
            "Provider order does not match the reserved request or known schema"
        )


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
            try:
                validate_order(
                    order,
                    request_id,
                    cents,
                    merchant_id=notes.get("sentinel_merchant_id", ""),
                )
                if order["status"] != "created" or order.get("receipt") != receipt:
                    raise ValueError("Unexpected creation response")
            except ValueError:
                return {
                    "status": "unknown",
                    "order_id": None,
                    "error": "Provider order response mismatch; reconciliation required",
                }
            return {"status": "created", "order_id": order["id"], "amount_paise": cents}
        except Exception as exc:
            # A timeout is not proof that an external side effect did not happen.
            logger.warning(
                "Gateway order outcome unknown; reconciliation required (%s)",
                type(exc).__name__,
            )
            return {
                "status": "unknown",
                "order_id": None,
                "error": "Reconciliation required",
            }
        finally:
            self.client.session.close()

    def reconcile(self, order_id, request_id, amount_paise, merchant_id=""):
        if not valid_reference(order_id):
            raise ValueError("Invalid provider order reference")
        if self.mock_mode:
            raise ValueError("Provider reconciliation requires test credentials")
        try:
            order = self.client.order.fetch(order_id, timeout=(3.05, 10))
        finally:
            self.client.session.close()
        validate_order(
            order, request_id, amount_paise, order_id=order_id, merchant_id=merchant_id
        )
        return {
            "order_id": order_id,
            "status": "captured" if order.get("status") == "paid" else "created",
        }

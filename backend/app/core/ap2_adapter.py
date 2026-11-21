"""
AP2 Mandate Adapter
======================
Google's Agent Payments Protocol (AP2) represents every agent purchase
as three signed Mandates -- Intent, Cart/Checkout, and Payment -- and
exists to guarantee three properties: Authorization (did the user
actually permit this), Authenticity (does the request reflect true,
non-hallucinated intent), and Accountability (an immutable audit
trail). See https://ap2-protocol.org/ and Google's announcement at
https://cloud.google.com/blog/products/ai-machine-learning/announcing-agents-to-payments-ap2-protocol

Sentinel's existing architecture maps onto this almost exactly:

    AP2 Intent Mandate   -> Sentinel AgentPolicy
    AP2 Cart Mandate      -> Sentinel TransactionRequest
    "Authorization"       -> rule_engine.py
    "Authenticity"        -> llm_reasoner.py (intent-drift detection)
    "Accountability"      -> audit_logger.py

This module is a DATA-MODEL adapter: it accepts AP2-shaped mandate
JSON and translates it into Sentinel's existing pipeline, so a real
AP2-speaking caller could use Sentinel as their authorization layer
without knowing anything about Sentinel's internal schema.

Explicitly OUT OF SCOPE, and deliberately not silently implied as
done: AP2 mandates are W3C Verifiable Credentials with real
cryptographic signatures (signed by the user's wallet or agent key).
This adapter does NOT verify those signatures -- it only translates
the JSON shape. Claiming working signature verification without
actually implementing real crypto would misrepresent a security
property of a payments system, which is worse than not having the
feature at all. `signature_verified` is carried through as an
explicit, honest field so a caller always knows whether that
verification happened -- it never silently defaults to "trusted."
"""

from __future__ import annotations
from dataclasses import dataclass, field
from typing import Optional
import uuid

from app.models.policy import AgentPolicy, TransactionRequest, TimeWindow, VelocityLimit


@dataclass
class MandateTranslationResult:
    policy: Optional[AgentPolicy]
    request: Optional[TransactionRequest]
    signature_verified: bool
    warnings: list = field(default_factory=list)


def intent_mandate_to_policy(
    intent_mandate: dict, agent_id: str, owner_human_id: str
) -> AgentPolicy:
    """
    Translates an AP2-shaped Intent Mandate into a Sentinel AgentPolicy.

    Expected shape (a reasonable, documented approximation of AP2's
    Intent Mandate fields -- not a copy of the official JSON-LD schema,
    since exact validation requires the AP2 SDK):

        {
          "max_amount": 2000.0,
          "currency": "INR",
          "allowed_merchant_categories": ["grocery"],
          "excluded_merchants": ["ShadyStore"],
          "valid_hours": {"start": 6, "end": 22},
          "max_transactions_per_day": 3,
          "recurrence_window_minutes": 1440
        }
    """
    valid_hours = intent_mandate.get("valid_hours", {})
    max_amount = float(intent_mandate.get("max_amount", 2000.0))
    return AgentPolicy(
        policy_id=f"pol_ap2_{uuid.uuid4().hex[:10]}",
        agent_id=agent_id,
        owner_human_id=owner_human_id,
        max_amount_per_txn=max_amount,
        max_daily_total=float(intent_mandate.get("max_daily_total", max_amount * 5)),
        allowed_categories=intent_mandate.get("allowed_merchant_categories", []),
        blocked_merchants=intent_mandate.get("excluded_merchants", []),
        time_window=TimeWindow(
            start_hour=valid_hours.get("start", 0),
            end_hour=valid_hours.get("end", 23),
        ),
        velocity=VelocityLimit(
            max_count=intent_mandate.get("max_transactions_per_day", 10),
            window_minutes=intent_mandate.get("recurrence_window_minutes", 1440),
        ),
    )


def cart_mandate_to_request(
    cart_mandate: dict, agent_id: str, raw_agent_goal: str
) -> TransactionRequest:
    """
    Translates an AP2-shaped Cart Mandate into a Sentinel TransactionRequest.

    Expected shape (approximation of AP2's Cart Mandate fields):

        {
          "merchant_name": "FreshMart",
          "merchant_category": "grocery",
          "total_amount": 450.0,
          "line_items_summary": "weekly grocery order",
          "requested_at": "2026-08-24T14:00:00",
          "requested_at_hour": 14
        }
    """
    return TransactionRequest(
        request_id=f"ap2_req_{uuid.uuid4().hex[:10]}",
        agent_id=agent_id,
        merchant_name=cart_mandate.get("merchant_name", "unknown"),
        merchant_category=cart_mandate.get("merchant_category", "unknown"),
        amount=float(cart_mandate.get("total_amount", 0.0)),
        requested_at_hour=int(cart_mandate.get("requested_at_hour", 12)),
        requested_at_iso=cart_mandate.get("requested_at", "2026-01-01T12:00:00"),
        stated_intent=cart_mandate.get("line_items_summary", ""),
        raw_agent_goal=raw_agent_goal,
    )


def translate_mandates(
    intent_mandate: dict,
    cart_mandate: dict,
    agent_id: str,
    owner_human_id: str,
) -> MandateTranslationResult:
    """
    Full translation: Intent Mandate + Cart Mandate -> (AgentPolicy, TransactionRequest),
    ready to hand to SentinelOrchestrator.evaluate_transaction() exactly like
    any other request. The orchestrator's APPROVE/HOLD/BLOCK verdict is what
    would inform whether an AP2 Payment Mandate gets generated.
    """
    warnings = []
    if "signature" not in intent_mandate and "signature" not in cart_mandate:
        warnings.append(
            "No signature field present on either mandate. This adapter does not verify "
            "cryptographic signatures in any case (see module docstring) -- flagging this "
            "explicitly so no caller mistakes translation for authentication."
        )

    policy = intent_mandate_to_policy(intent_mandate, agent_id, owner_human_id)
    raw_goal = intent_mandate.get("goal_description", "")
    request = cart_mandate_to_request(cart_mandate, agent_id, raw_goal)

    return MandateTranslationResult(
        policy=policy,
        request=request,
        signature_verified=False,
        warnings=warnings,
    )

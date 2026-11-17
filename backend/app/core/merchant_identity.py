"""Merchant attributes come from human-controlled identity, never an agent assertion.

Operator attestation is not acquirer verification or proof of settlement routing.
"""

import os
from app.models.db import MerchantIdentity, TransactionRecord, PaymentRecord


def resolve(db, policy, request):
    if not request.merchant_id:
        return (
            "Production requires a registered merchant_id"
            if os.environ.get("SENTINEL_ENV") == "production"
            else None
        )
    row = (
        db.query(MerchantIdentity)
        .filter_by(merchant_id=request.merchant_id, owner_id=policy.owner_human_id)
        .populate_existing()
        .with_for_update()
        .first()
    )
    if row is None or not row.active:
        return "Merchant identity is unknown, inactive or outside owner scope"
    request.merchant_name, request.merchant_category = row.legal_name, row.category
    request.merchant_version = row.version
    return None


def dispatch_binding(db, request_id):
    tx = db.query(TransactionRecord).filter_by(request_id=request_id).first()
    if not tx or not tx.merchant_id:
        return (
            "No registered merchant identity for dispatch"
            if os.environ.get("SENTINEL_ENV") == "production"
            else None
        )
    payment = db.get(PaymentRecord, request_id)
    row = (
        db.query(MerchantIdentity)
        .filter_by(merchant_id=tx.merchant_id)
        .populate_existing()
        .with_for_update()
        .first()
    )
    if (
        row is None
        or not row.active
        or row.version != payment.authorized_merchant_version
    ):
        return "Merchant identity changed after approval; cancel and submit a fresh request"
    return None

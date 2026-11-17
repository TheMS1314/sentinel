"""Seed reviewed historical ledger states only in the dedicated disposable eval DB."""

from app.models.db import MerchantIdentity, PaymentRecord
from app.models.policy import Decision
from app.core.audit_logger import AuditLogger
from app.core.validation import paise


def seed(db, case, policy, agent_id, suffix, request_factory):
    if db.bind.dialect.name != "sqlite" or "sentinel-eval-" not in str(
        db.bind.url.database
    ):
        raise RuntimeError(
            "Feedback fixtures are restricted to the isolated evaluation database"
        )
    snapshot = case.get("identity_snapshot")
    if snapshot:
        db.merge(
            MerchantIdentity(
                merchant_id=snapshot["merchant_id"],
                owner_id=policy.owner_human_id,
                legal_name=snapshot["legal_name"],
                category=snapshot["category"],
                domain=snapshot["domain"],
                version=snapshot["version"],
                active=True,
                approved_by=snapshot["approved_by"],
                operator_reference="Offline replay of immutable operator attestation",
            )
        )
    audit = AuditLogger(db)
    for row in case.get("history_records", []):
        req = request_factory(row["request"], agent_id, suffix)
        audit.log_transaction(req, scenario_tag="curated_history")
        d = audit.log_decision(
            Decision(
                req.request_id,
                row["verdict"],
                ["Observed historical decision; not re-evaluated"],
                [],
                "recorded_history",
            ),
            policy,
        )
        d.human_override = row.get("human_override")
        if row["payment_state"] != "not_sent":
            db.add(
                PaymentRecord(
                    request_id=req.request_id,
                    state=row["payment_state"],
                    amount_paise=paise(req.amount),
                    budget_day=row.get("budget_day") or req.requested_at_iso[:10],
                )
            )
    db.commit()

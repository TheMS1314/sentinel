"""Owner-controlled merchant directory; service agents cannot enroll or change payees."""

from datetime import timezone
import uuid
from fastapi import APIRouter, Depends, HTTPException
from pydantic import Field, field_validator
from sqlalchemy.orm import Session
from app.core.auth import Principal, require_api_key, assert_current_principal
from app.core.rate_limit import enforce_rate_limit
from app.core.schemas import StrictModel, ID
from app.core.intelligence_schemas import normalize_domain
from app.core.intelligence import GUARD_LOCK, control_event, processing_time
from app.core.locking import agent_transaction
from app.models.db import get_session, MerchantIdentity

router = APIRouter(
    prefix="/merchants",
    dependencies=[Depends(require_api_key), Depends(enforce_rate_limit)],
)


class MerchantIn(StrictModel):
    merchant_id: ID = Field(default_factory=lambda: "merchant_" + uuid.uuid4().hex)
    owner_id: ID
    legal_name: str = Field(min_length=1, max_length=200)
    category: str = Field(min_length=1, max_length=100)
    domain: str = Field(min_length=3, max_length=253)
    operator_reference: str = Field(min_length=1, max_length=2000)
    expected_version: int = Field(ge=0)
    active: bool = True

    @field_validator("domain")
    @classmethod
    def domain_value(cls, value):
        return normalize_domain(value)


def view(row):
    return {
        k: getattr(row, k)
        for k in (
            "merchant_id",
            "owner_id",
            "legal_name",
            "category",
            "domain",
            "operator_reference",
            "active",
            "version",
            "approved_by",
        )
    } | {
        "updated_at": row.updated_at.replace(tzinfo=timezone.utc).isoformat(),
        "verification_level": "operator_attested",
        "settlement_verified": False,
    }


@router.get("")
def list_merchants(
    db: Session = Depends(get_session), principal: Principal = Depends(require_api_key)
):
    query = db.query(MerchantIdentity)
    principal.require("admin", "owner", "reviewer")
    if principal.role != "admin":
        query = query.filter_by(owner_id=principal.owner_id)
    return [
        view(r) for r in query.order_by(MerchantIdentity.updated_at.desc()).limit(200)
    ]


@router.post("")
def save_merchant(
    body: MerchantIn,
    db: Session = Depends(get_session),
    principal: Principal = Depends(require_api_key),
):
    principal.require("admin", "owner")
    if principal.role != "admin" and body.owner_id != principal.owner_id:
        raise HTTPException(403, "Cannot enroll another owner's merchant")
    with agent_transaction(db, GUARD_LOCK):
        assert_current_principal(principal, db)
        row = (
            db.query(MerchantIdentity)
            .filter_by(merchant_id=body.merchant_id)
            .populate_existing()
            .with_for_update()
            .first()
        )
        before = view(row) if row else None
        if row is None:
            if body.expected_version != 0:
                raise HTTPException(409, "New merchant requires expected_version=0")
            row = MerchantIdentity(
                merchant_id=body.merchant_id, owner_id=body.owner_id, version=0
            )
            db.add(row)
        elif row.owner_id != body.owner_id:
            raise HTTPException(403, "Merchant ownership cannot be reassigned")
        if row.version != body.expected_version:
            raise HTTPException(409, "Merchant changed; refresh before saving")
        for key, value in body.model_dump(exclude={"expected_version"}).items():
            setattr(row, key, value)
        row.version += 1
        row.approved_by, row.updated_at = principal.subject, processing_time(db)
        after = view(row)
        control_event(
            db, principal.subject, "merchant_identity", row.merchant_id, before, after
        )
    return after

"""Admin mappings and refresh: network IO runs outside a database transaction."""

import uuid
from typing import Literal
from fastapi import APIRouter, Depends, HTTPException
from pydantic import Field
from sqlalchemy.orm import Session
from app.core.auth import Principal, require_api_key, assert_current_principal
from app.core.rate_limit import enforce_rate_limit
from app.core.schemas import StrictModel, ID
from app.core.locking import agent_transaction
from app.core.intelligence import GUARD_LOCK, control_event, processing_time
from app.core import providers
from app.models.db import (
    get_session,
    MerchantIdentity,
    ProviderBinding,
    ProviderEvidence,
)

router = APIRouter(
    prefix="/providers",
    dependencies=[Depends(require_api_key), Depends(enforce_rate_limit)],
)


class BindingIn(StrictModel):
    merchant_id: ID
    provider: Literal["recorded_future", "riskrecon"]
    external_id: str = Field(
        min_length=1, max_length=253, pattern=r"^[A-Za-z0-9_.:-]+$"
    )
    expected_version: int = Field(ge=0)


@router.get("")
def status(
    db: Session = Depends(get_session), principal: Principal = Depends(require_api_key)
):
    principal.require("admin")
    return {
        "configuration": providers.configuration(),
        "bindings": [
            dict(
                binding_id=b.binding_id,
                merchant_id=b.merchant_id,
                provider=b.provider,
                external_id=b.external_id,
                version=b.version,
            )
            for b in db.query(ProviderBinding)
            .order_by(ProviderBinding.updated_at.desc())
            .limit(100)
        ],
    }


@router.post("/bindings")
def bind(
    body: BindingIn,
    db: Session = Depends(get_session),
    principal: Principal = Depends(require_api_key),
):
    principal.require("admin")
    with agent_transaction(db, GUARD_LOCK):
        assert_current_principal(principal, db)
        merchant = db.get(MerchantIdentity, body.merchant_id)
        if merchant is None or not merchant.active:
            raise HTTPException(422, "An active registered merchant is required")
        if (
            body.provider == "recorded_future"
            and body.external_id != "idn:" + merchant.domain
        ):
            raise HTTPException(
                422, "Domain entity must match the registered merchant domain"
            )
        row = (
            db.query(ProviderBinding)
            .filter_by(merchant_id=body.merchant_id, provider=body.provider)
            .first()
        )
        if (row.version if row else 0) != body.expected_version:
            raise HTTPException(409, "Binding version changed")
        before = dict(external_id=row.external_id, version=row.version) if row else None
        if row is None:
            row = ProviderBinding(
                binding_id="binding_" + uuid.uuid4().hex,
                merchant_id=body.merchant_id,
                provider=body.provider,
                version=0,
            )
            db.add(row)
        row.external_id, row.updated_by, row.version = (
            body.external_id,
            principal.subject,
            row.version + 1,
        )
        row.updated_at = processing_time(db)
        result = dict(binding_id=row.binding_id, version=row.version)
        control_event(
            db,
            principal.subject,
            "provider_binding",
            row.binding_id,
            before,
            dict(result, external_id=row.external_id),
        )
    return result


@router.post("/bindings/{binding_id}/refresh")
def refresh(
    binding_id: str,
    db: Session = Depends(get_session),
    principal: Principal = Depends(require_api_key),
):
    principal.require("admin")
    row = db.get(ProviderBinding, binding_id)
    if row is None:
        raise HTTPException(404, "Unknown binding")
    provider, external, version, mid = (
        row.provider,
        row.external_id,
        row.version,
        row.merchant_id,
    )
    mv = db.get(MerchantIdentity, mid).version
    db.rollback()
    result = providers.fetch(provider, external)
    with agent_transaction(db, GUARD_LOCK):
        assert_current_principal(principal, db)
        current = db.get(ProviderBinding, binding_id, populate_existing=True)
        merchant = db.get(MerchantIdentity, mid, populate_existing=True)
        if current.version != version or merchant.version != mv or not merchant.active:
            raise HTTPException(409, "Binding changed during refresh; discard response")
        evidence = ProviderEvidence(
            evidence_id="evidence_" + uuid.uuid4().hex,
            binding_id=binding_id,
            binding_version=version,
            merchant_version=mv,
            **result,
        )
        db.add(evidence)
        control_event(
            db,
            principal.subject,
            "provider_refresh",
            binding_id,
            None,
            dict(
                evidence_id=evidence.evidence_id,
                status=evidence.status,
                error=evidence.error,
            ),
        )
        output = dict(
            evidence_id=evidence.evidence_id,
            status=evidence.status,
            error=evidence.error,
        )
    return output

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
from app.core.intelligence import GUARD_LOCK, control_event, processing_time, utc
from app.core.provider_refresh import (
    run_one,
    RefreshConflict,
    RefreshMissing,
    scheduler_enabled,
)
from app.core import providers
from app.models.db import (
    get_session,
    MerchantIdentity,
    ProviderBinding,
)

router = APIRouter(
    prefix="/providers",
    dependencies=[Depends(require_api_key), Depends(enforce_rate_limit)],
)


class BindingIn(StrictModel):
    merchant_id: ID
    provider: Literal["recorded_future", "riskrecon"]
    external_id: str = Field(
        min_length=1, max_length=300, pattern=r"^[A-Za-z0-9_.:-]+$"
    )
    expected_version: int = Field(ge=0)
    auto_refresh: bool = False
    refresh_interval_seconds: int = Field(default=300, ge=60, le=86400)


@router.get("")
def status(
    db: Session = Depends(get_session), principal: Principal = Depends(require_api_key)
):
    principal.require("admin")
    now = processing_time(db)
    return {
        "configuration": providers.configuration(),
        "scheduler_enabled": scheduler_enabled(),
        "bindings": [
            dict(
                binding_id=b.binding_id,
                merchant_id=b.merchant_id,
                provider=b.provider,
                external_id=b.external_id,
                version=b.version,
                merchant_version=b.merchant_version,
                requires_remap=b.merchant_version != merchant_version,
                auto_refresh=b.auto_refresh,
                refresh_interval_seconds=b.refresh_interval_seconds,
                refresh_status=b.refresh_status,
                refresh_error=b.refresh_error,
                refresh_next_at=utc(b.refresh_next_at).isoformat(),
                refresh_in_progress=bool(
                    b.refresh_token
                    and b.refresh_lease_expires_at
                    and utc(b.refresh_lease_expires_at) > now
                ),
            )
            for b, merchant_version in db.query(
                ProviderBinding, MerchantIdentity.version
            )
            .join(MerchantIdentity)
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
        row.merchant_version = merchant.version
        row.auto_refresh = body.auto_refresh
        row.refresh_interval_seconds = body.refresh_interval_seconds
        row.refresh_next_at = row.updated_at
        row.refresh_token, row.refresh_lease_expires_at = None, None
        row.refresh_status, row.refresh_error = (
            "pending" if body.auto_refresh else "never",
            None,
        )
        result = dict(binding_id=row.binding_id, version=row.version)
        control_event(
            db,
            principal.subject,
            "provider_binding",
            row.binding_id,
            before,
            dict(
                result,
                external_id=row.external_id,
                merchant_version=row.merchant_version,
                auto_refresh=row.auto_refresh,
                refresh_interval_seconds=row.refresh_interval_seconds,
            ),
        )
    return result


@router.post("/bindings/{binding_id}/refresh")
def refresh(
    binding_id: str,
    db: Session = Depends(get_session),
    principal: Principal = Depends(require_api_key),
):
    principal.require("admin")
    try:
        result = run_one(db, binding_id, principal)
    except RefreshMissing as exc:
        raise HTTPException(404, str(exc)) from None
    except RefreshConflict as exc:
        raise HTTPException(409, str(exc)) from None
    if result is None:
        raise HTTPException(409, "Provider binding needs remapping before refresh")
    return result

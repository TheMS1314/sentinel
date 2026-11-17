"""Administrator-only shared authority lifecycle; keys are never listed."""

from datetime import datetime, timezone
from typing import Literal
from fastapi import APIRouter, Depends, HTTPException, Response
from pydantic import Field, model_validator
from sqlalchemy.orm import Session
from app.core.auth import Principal, require_api_key, database_authority
from app.core.credential_admin import change, metadata
from app.core.schemas import StrictModel, ID
from app.core.rate_limit import enforce_rate_limit
from app.models.db import get_session, ServiceCredential

router = APIRouter(
    prefix="/credentials",
    dependencies=[Depends(require_api_key), Depends(enforce_rate_limit)],
)


class CredentialChange(StrictModel):
    subject: ID
    action: Literal["create", "rotate", "scope", "revoke"]
    expected_version: int = Field(ge=0)
    role: Literal["admin", "owner", "reviewer", "agent"] = "agent"
    owner_id: str = Field(default="", max_length=128, pattern=r"^[A-Za-z0-9_.:-]*$")
    agent_ids: list[ID] = Field(default_factory=list, max_length=500)
    expires_at: datetime | None = None

    @model_validator(mode="after")
    def scope_and_expiry(self):
        if self.action in ("create", "scope"):
            if self.role in ("owner", "reviewer") and not self.owner_id:
                raise ValueError("Owner scope required")
            if self.role == "agent" and not self.agent_ids:
                raise ValueError("Agent scope required")
        if self.expires_at and (
            self.expires_at.tzinfo is None
            or self.expires_at <= datetime.now(timezone.utc)
        ):
            raise ValueError("Use a future timezone-aware expiry")
        if self.expires_at:
            self.expires_at = self.expires_at.astimezone(timezone.utc)
        return self


def authorize(principal):
    principal.require("admin")
    if not database_authority():
        raise HTTPException(
            409, "Enable shared database authority to manage credentials"
        )


@router.get("")
def list_credentials(
    db: Session = Depends(get_session), principal: Principal = Depends(require_api_key)
):
    authorize(principal)
    return [
        metadata(r)
        for r in db.query(ServiceCredential)
        .order_by(ServiceCredential.subject)
        .limit(500)
    ]


@router.post("")
def change_credential(
    body: CredentialChange,
    response: Response,
    db: Session = Depends(get_session),
    principal: Principal = Depends(require_api_key),
):
    authorize(principal)
    response.headers["Cache-Control"] = "no-store"
    return change(db, principal, body)

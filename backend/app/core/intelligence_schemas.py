"""Operator-managed evidence. Provider brands are never accepted as fake integrations."""

from datetime import datetime, timezone
import ipaddress
import re
from typing import Literal

from pydantic import Field, field_validator
from app.core.schemas import ID, StrictModel


def normalize_domain(value):
    value = value.strip().rstrip(".").lower()
    if any(c in value for c in "/:@*?#"):
        raise ValueError("Use an exact domain, without a URL, wildcard, port or path")
    try:
        ipaddress.ip_address(value)
    except ValueError:
        pass
    else:
        raise ValueError("Use a domain, not an IP address")
    try:
        value = value.encode("idna").decode("ascii")
    except UnicodeError as exc:
        raise ValueError("Invalid domain") from exc
    if (
        len(value) > 253
        or "." not in value
        or any(
            not re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?", part)
            for part in value.split(".")
        )
    ):
        raise ValueError("Invalid domain")
    return value


class EvidenceIn(StrictModel):
    domain: str = Field(min_length=3, max_length=253)
    source: Literal["operator", "synthetic"] = "operator"
    evidence: str = Field(min_length=1, max_length=2000)
    expires_at: datetime

    @field_validator("domain")
    @classmethod
    def domain_value(cls, value):
        return normalize_domain(value)

    @field_validator("expires_at")
    @classmethod
    def future_expiry(cls, value):
        if value.tzinfo is None or value <= datetime.now(timezone.utc):
            raise ValueError("expires_at must be a future timezone-aware timestamp")
        return value.astimezone(timezone.utc)


class MerchantProfileIn(EvidenceIn):
    owner_id: ID
    merchant_name: str = Field(min_length=1, max_length=200)
    cyber_risk_score: int = Field(ge=0, le=100)


class IndicatorIn(EvidenceIn):
    indicator_id: ID
    confirmed: bool = False
    active: bool = True


class GuardIn(StrictModel):
    expected_version: int = Field(ge=0)
    frozen: bool = False
    window_seconds: int = Field(default=60, ge=1, le=3600)
    max_dispatches: int | None = Field(default=None, ge=1, le=100000)
    max_amount_paise: int | None = Field(default=None, ge=1, le=100_000_000_000)
    reason: str = Field(min_length=1, max_length=2000)

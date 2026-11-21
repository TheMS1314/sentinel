"""Durable single-flight refresh; external IO never holds a transaction or claim lock."""

from dataclasses import dataclass
from datetime import timedelta
import os
import uuid
from sqlalchemy import or_, func
from app.core.auth import assert_current_principal
from app.core.intelligence import GUARD_LOCK, control_event, processing_time, utc
from app.core.locking import agent_transaction
from app.core.settings import integer
from app.core import providers
from app.models.db import ProviderBinding, ProviderEvidence, MerchantIdentity


class RefreshConflict(Exception):
    pass


class RefreshMissing(RefreshConflict):
    pass


@dataclass(frozen=True)
class RefreshClaim:
    binding_id: str
    version: int
    merchant_id: str
    merchant_version: int
    provider: str
    external_id: str
    token: str
    generation: int


def scheduler_enabled():
    value = os.environ.get("SENTINEL_PROVIDER_REFRESH_ENABLED", "0")
    if value not in ("0", "1"):
        raise RuntimeError("SENTINEL_PROVIDER_REFRESH_ENABLED must be 0 or 1")
    return value == "1"


def claim(db, binding_id=None, principal=None):
    # A short shared claim transaction also serializes binding edits and dispatch
    # evidence checks. No lock is retained during network IO.
    with agent_transaction(db, GUARD_LOCK):
        if principal is not None:
            assert_current_principal(principal, db)
        now = processing_time(db)
        q = db.query(ProviderBinding).populate_existing()
        if binding_id:
            q = q.filter_by(binding_id=binding_id)
        else:
            if not scheduler_enabled():
                return None
            q = (
                q.join(MerchantIdentity)
                .filter(
                    ProviderBinding.auto_refresh.is_(True),
                    ProviderBinding.refresh_next_at <= now,
                    MerchantIdentity.active.is_(True),
                    or_(
                        ProviderBinding.refresh_token.is_(None),
                        ProviderBinding.refresh_lease_expires_at.is_(None),
                        ProviderBinding.refresh_lease_expires_at <= now,
                    ),
                )
                .order_by(ProviderBinding.refresh_next_at, ProviderBinding.binding_id)
            )
        row = q.with_for_update(of=ProviderBinding, skip_locked=True).first()
        if row is None:
            if binding_id:
                if db.get(ProviderBinding, binding_id) is None:
                    raise RefreshMissing("Unknown provider binding")
                raise RefreshConflict("Provider binding is busy")
            return None
        merchant = db.get(MerchantIdentity, row.merchant_id, populate_existing=True)
        if merchant is None or not merchant.active:
            raise RefreshConflict("Merchant identity is inactive")
        if (
            row.refresh_token
            and row.refresh_lease_expires_at
            and utc(row.refresh_lease_expires_at) > now
        ):
            raise RefreshConflict("A provider refresh is already in progress")
        if row.merchant_version != merchant.version:
            row.refresh_status, row.refresh_error = (
                "binding_changed",
                "Merchant identity changed; explicitly review and save this mapping",
            )
            row.refresh_token, row.refresh_lease_expires_at = None, None
            row.refresh_next_at = now + timedelta(seconds=600)
            return None
        # After a directory domain change an RF binding must be explicitly remapped.
        # Discarding an old-domain response afterward would already have disclosed it.
        if (
            row.provider == "recorded_future"
            and row.external_id != "idn:" + merchant.domain
        ):
            row.refresh_status, row.refresh_error = (
                "binding_changed",
                "Registered domain changed; remap before refresh",
            )
            row.refresh_token, row.refresh_lease_expires_at = None, None
            row.refresh_next_at = now + timedelta(seconds=600)
            return None
        row.refresh_generation += 1
        row.refresh_token = uuid.uuid4().hex
        row.refresh_lease_expires_at = now + timedelta(
            seconds=integer(
                "SENTINEL_PROVIDER_LEASE_SECONDS", 60, minimum=15, maximum=120
            )
        )
        row.refresh_status, row.refresh_error = "processing", None
        result = RefreshClaim(
            row.binding_id,
            row.version,
            row.merchant_id,
            merchant.version,
            row.provider,
            row.external_id,
            row.refresh_token,
            row.refresh_generation,
        )
    return result


def finish(db, claimed, result, principal=None):
    conflict = None
    output = None
    with agent_transaction(db, GUARD_LOCK):
        if principal is not None:
            assert_current_principal(principal, db)
        row = db.get(ProviderBinding, claimed.binding_id, populate_existing=True)
        now = processing_time(db)
        if (
            row is None
            or row.refresh_token != claimed.token
            or row.refresh_generation != claimed.generation
        ):
            raise RefreshConflict("Refresh was superseded; discard response")
        merchant = db.get(MerchantIdentity, claimed.merchant_id, populate_existing=True)
        if (
            row.merchant_version != claimed.merchant_version
            or row.version != claimed.version
            or merchant is None
            or merchant.version != claimed.merchant_version
            or not merchant.active
        ):
            conflict = "Binding or identity changed during refresh; discard response"
        elif (
            row.refresh_lease_expires_at is None
            or utc(row.refresh_lease_expires_at) <= now
        ):
            conflict = "Refresh lease expired; discard response"
        if conflict:
            row.refresh_status, row.refresh_error = "discarded", conflict
            row.refresh_token, row.refresh_lease_expires_at = None, None
            row.refresh_next_at = now + timedelta(seconds=60)
        else:
            result = dict(result)
            # Use a DB completion clock and a durable generation, not host start
            # clocks, to order append-only evidence across replicas.
            result["fetched_at"] = now
            if result.get("observed_at") and utc(
                result["observed_at"]
            ) > now + timedelta(minutes=5):
                result.update(
                    status="error",
                    error="Future source timestamp against database clock",
                    observed_at=None,
                    score=None,
                    expires_at=now,
                )
            elif result["status"] in ("current", "stale"):
                if result.get("observed_at") is None:
                    result.update(
                        status="error", error="Missing source timestamp", expires_at=now
                    )
                else:
                    result["expires_at"] = min(
                        utc(result["expires_at"]),
                        now
                        + timedelta(
                            seconds=integer(
                                "SENTINEL_FEED_TTL_SECONDS", 3600, maximum=86400
                            )
                        ),
                        utc(result["observed_at"])
                        + timedelta(
                            days=integer(
                                "SENTINEL_FEED_MAX_SOURCE_AGE_DAYS", 7, maximum=30
                            )
                        ),
                    )
                    if result["expires_at"] <= now:
                        result["status"] = "stale"
            watermark = (
                db.query(func.max(ProviderEvidence.observed_at))
                .filter_by(
                    binding_id=row.binding_id,
                    binding_version=row.version,
                    merchant_version=merchant.version,
                )
                .scalar()
            )
            if (
                watermark
                and result.get("observed_at")
                and utc(result["observed_at"]) < utc(watermark)
            ):
                result.update(
                    status="error", error="Source timestamp regressed", expires_at=now
                )
            evidence = ProviderEvidence(
                evidence_id="evidence_" + uuid.uuid4().hex,
                binding_id=row.binding_id,
                binding_version=row.version,
                merchant_version=merchant.version,
                refresh_generation=claimed.generation,
                **result,
            )
            db.add(evidence)
            delay = row.refresh_interval_seconds
            if evidence.status == "current":
                delay = max(
                    30,
                    min(
                        delay,
                        int((utc(evidence.expires_at) - now).total_seconds()) - 15,
                    ),
                )
            else:
                delay = max(60, min(delay, 600))
            row.refresh_next_at = now + timedelta(seconds=delay)
            row.refresh_status, row.refresh_error = evidence.status, evidence.error
            row.refresh_token, row.refresh_lease_expires_at = None, None
            output = dict(
                evidence_id=evidence.evidence_id,
                status=evidence.status,
                error=evidence.error,
            )
            control_event(
                db,
                principal.subject if principal else "system-provider-refresh",
                "provider_refresh",
                row.binding_id,
                None,
                dict(
                    output, generation=claimed.generation, scheduled=principal is None
                ),
            )
    if conflict:
        raise RefreshConflict(conflict)
    return output


def run_one(db, binding_id=None, principal=None):
    claimed = claim(db, binding_id, principal)
    if claimed is None:
        return None
    # claim() committed. No database connection or transaction is held here.
    try:
        result = providers.fetch(claimed.provider, claimed.external_id)
    except Exception as exc:
        now = processing_time(db)
        db.rollback()
        result = dict(
            status="error",
            score=None,
            observed_at=None,
            fetched_at=now,
            expires_at=now,
            response_hash=None,
            error=type(exc).__name__,
        )
    return finish(db, claimed, result, principal)

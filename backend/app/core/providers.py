"""Read-only, bounded commercial adapters. Cached evidence can restrict authority only."""

import hashlib
import json
import math
import os
from datetime import datetime, timedelta, timezone
from urllib.parse import quote
from threading import BoundedSemaphore
import requests
from app.core.intelligence import utc, processing_time
from app.core.settings import integer
from app.models.db import ProviderBinding, ProviderEvidence

PROVIDERS = ("recorded_future", "riskrecon")
_slots = BoundedSemaphore(2)


def configuration():
    return {
        name: "configured_unverified" if os.environ.get(key) else "not_configured"
        for name, key in [
            ("recorded_future", "RECORDED_FUTURE_API_TOKEN"),
            ("riskrecon", "RISKRECON_API_TOKEN"),
        ]
    }


def timestamp(value):
    if not isinstance(value, str):
        raise ValueError("Missing source timestamp")
    result = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if result.tzinfo is None:
        raise ValueError("Source timestamp needs timezone")
    return utc(result)


def number(value, low, high):
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not low <= value <= high
        or not math.isfinite(value)
    ):
        raise ValueError("Invalid source score")
    try:
        return float(value)
    except OverflowError:
        raise ValueError("Source score overflows numeric bounds") from None


def normalize(provider, external_id, payload):
    if provider == "recorded_future":
        data = payload["data"]
        if data["entity"]["id"] != external_id:
            raise ValueError("Source entity mismatch")
        return number(data["risk"]["score"], 0, 99), timestamp(
            data["timestamps"]["lastSeen"]
        )
    if provider == "riskrecon":
        toes = payload["toes"]
        if (
            not isinstance(toes, list)
            or len(toes) != 1
            or toes[0]["toe_id"] != external_id
        ):
            raise ValueError("Source entity mismatch or ambiguous portfolio member")
        row = toes[0]
        return number(row["overall_rating_numeric"], 0, 10), timestamp(
            row["last_analysis_ts"]
        )
    raise ValueError("Unsupported provider")


def fetch(provider, external_id):
    """No caller-controlled origin, redirects, retries, proxies or unbounded body."""
    now = datetime.now(timezone.utc)
    result = dict(
        status="error",
        score=None,
        observed_at=None,
        fetched_at=now,
        expires_at=now,
        response_hash=None,
        error=None,
    )
    key = os.environ.get(
        "RECORDED_FUTURE_API_TOKEN"
        if provider == "recorded_future"
        else "RISKRECON_API_TOKEN",
        "",
    )
    if not key:
        return dict(
            result, status="not_configured", error="Provider credential missing"
        )
    if provider == "recorded_future":
        url = "https://api.recordedfuture.com/v2/domain/" + quote(external_id, safe="")
        headers, params = {"X-RFToken": key}, {"fields": "entity,risk,timestamps"}
    elif provider == "riskrecon":
        # Publicly documented sandbox origin. Do not guess production entitlement URLs.
        url = "https://api.sandbox.riskrecon.com/v0/portfolio/member/toe_id/" + quote(
            external_id, safe=""
        )
        headers, params = {"Authorization": "Bearer " + key}, {}
    else:
        raise ValueError("Unsupported provider")
    if not _slots.acquire(blocking=False):
        return dict(result, error="Provider refresh capacity unavailable")
    try:
        with requests.Session() as session:
            session.trust_env = False
            with session.get(
                url,
                headers=headers,
                params=params,
                timeout=(3, 5),
                allow_redirects=False,
                stream=True,
            ) as response:
                if response.status_code != 200:
                    return dict(
                        result, error="http_status_" + str(response.status_code)
                    )
                raw = bytearray()
                for chunk in response.iter_content(8192):
                    raw.extend(chunk)
                    if len(raw) > 262144:
                        raise ValueError("Source body exceeds 256KiB limit")
                score, observed = normalize(provider, external_id, json.loads(raw))
                if observed > now + timedelta(minutes=5):
                    raise ValueError("Future source timestamp")
                expires = min(
                    now
                    + timedelta(
                        seconds=integer(
                            "SENTINEL_FEED_TTL_SECONDS", 3600, maximum=86400
                        )
                    ),
                    observed
                    + timedelta(
                        days=integer("SENTINEL_FEED_MAX_SOURCE_AGE_DAYS", 7, maximum=30)
                    ),
                )
                return dict(
                    result,
                    status="current" if expires > now else "stale",
                    score=score,
                    observed_at=observed,
                    expires_at=expires,
                    response_hash=hashlib.sha256(raw).hexdigest(),
                )
    except (requests.RequestException, ValueError, KeyError, TypeError) as exc:
        # Never persist exception text: it can contain credentials or licensed bodies.
        return dict(result, error=type(exc).__name__)
    finally:
        _slots.release()


def required_feeds():
    required = {
        v.strip()
        for v in os.environ.get("SENTINEL_REQUIRED_FEEDS", "").split(",")
        if v.strip()
    }
    if not required.issubset(PROVIDERS):
        raise ValueError("Unsupported required feed")
    return required


def assessment(db, merchant):
    required = required_feeds()
    bindings = (
        db.query(ProviderBinding).filter_by(merchant_id=merchant.merchant_id).all()
        if merchant
        else []
    )
    names = required | {b.provider for b in bindings}
    now = processing_time(db)
    reports = []
    for name in sorted(names):
        binding = next((b for b in bindings if b.provider == name), None)
        evidence = (
            db.query(ProviderEvidence)
            .filter_by(binding_id=binding.binding_id)
            .order_by(
                ProviderEvidence.refresh_generation.desc(),
                ProviderEvidence.fetched_at.desc(),
                ProviderEvidence.evidence_id.desc(),
            )
            .first()
            if binding
            else None
        )
        state = "missing"
        if evidence:
            state = evidence.status
            if (
                binding.merchant_version != merchant.version
                or binding.version != evidence.binding_version
                or merchant.version != evidence.merchant_version
            ):
                state = "binding_changed"
            elif utc(evidence.expires_at) <= now and state == "current":
                state = "stale"
        high = (
            evidence is not None
            and evidence.score is not None
            and (
                (name == "recorded_future" and evidence.score >= 70)
                or (name == "riskrecon" and evidence.score < 5)
            )
        )
        reports.append(
            dict(
                provider=name,
                status=state,
                evidence_id=evidence.evidence_id if evidence else None,
                score=evidence.score if evidence else None,
                meaning="domain threat score (0–99), not fraud probability"
                if name == "recorded_future"
                else "cyber rating (0–10, higher is better), not fraud probability",
                hold=state != "current" or high,
                expires_at=utc(evidence.expires_at).isoformat() if evidence else None,
            )
        )
    return reports

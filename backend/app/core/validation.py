"""Boundary validation shared by HTTP, queue and domain callers."""

from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
import re
import unicodedata


def merchant_key(name: str) -> str:
    """Compare names consistently while preserving punctuation and word boundaries."""
    return " ".join(unicodedata.normalize("NFKC", name).casefold().split())


def paise(value) -> int:
    try:
        number = Decimal(str(value))
        if not number.is_finite() or number <= 0 or number > Decimal("1000000000"):
            raise ValueError(
                "amount must be positive, finite and at most INR 1 billion"
            )
        cents = number * 100
        if cents != cents.to_integral_value():
            raise ValueError("money supports at most two decimal places")
        return int(cents)
    except (InvalidOperation, TypeError):
        raise ValueError("invalid money amount") from None


def identifier(value: str):
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9_.:-]{1,128}", value):
        raise ValueError("invalid identifier")
    return value


def utc_timestamp(value: str) -> datetime:
    try:
        ts = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return (
            ts.replace(tzinfo=timezone.utc)
            if ts.tzinfo is None
            else ts.astimezone(timezone.utc)
        )
    except (ValueError, AttributeError, TypeError):
        raise ValueError("timestamp must be ISO 8601") from None


def validate_request(request):
    identifier(request.request_id)
    identifier(request.agent_id)
    paise(request.amount)
    if (
        not isinstance(request.requested_at_hour, int)
        or not 0 <= request.requested_at_hour <= 23
    ):
        raise ValueError("requested_at_hour must be 0..23")
    ts = utc_timestamp(request.requested_at_iso)
    if ts.hour != request.requested_at_hour:
        raise ValueError("requested hour must match the UTC timestamp")
    request.requested_at_iso = ts.isoformat()
    for key, maximum in [
        ("merchant_name", 200),
        ("merchant_category", 100),
        ("stated_intent", 4000),
        ("raw_agent_goal", 4000),
    ]:
        value = getattr(request, key)
        if (
            not isinstance(value, str)
            or len(value) > maximum
            or (key != "raw_agent_goal" and not value.strip())
        ):
            raise ValueError(f"invalid {key}")


def validate_policy(policy):
    identifier(policy.policy_id)
    identifier(policy.agent_id)
    identifier(policy.owner_human_id)
    paise(policy.max_amount_per_txn)
    paise(policy.max_daily_total)
    if (
        not 0 <= policy.time_window.start_hour <= 23
        or not 0 <= policy.time_window.end_hour <= 23
    ):
        raise ValueError("policy hours must be 0..23")
    if (
        not 1 <= policy.velocity.max_count <= 100000
        or not 1 <= policy.velocity.window_minutes <= 43200
    ):
        raise ValueError("invalid velocity bounds")
    for values in [
        policy.allowed_categories,
        policy.blocked_merchants,
        policy.allowed_merchants or [],
    ]:
        if len(values) > 200 or any(
            not isinstance(v, str) or not v.strip() or len(v) > 200 for v in values
        ):
            raise ValueError("invalid policy list")
    if not policy.allowed_categories:
        raise ValueError("at least one allowed category is required")
    if not policy.authorized_goal.strip() or len(policy.authorized_goal) > 4000:
        raise ValueError("a bounded authorized goal is required")

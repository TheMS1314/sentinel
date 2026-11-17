"""Small validated runtime limits. Invalid configuration fails startup, not a payment."""

import os
from datetime import datetime, timedelta, timezone
from sqlalchemy.engine import make_url


def integer(name, default, minimum=1, maximum=1000000):
    try:
        value = int(os.environ.get(name, str(default)))
    except ValueError as exc:
        raise RuntimeError(f"{name} must be an integer") from exc
    if not minimum <= value <= maximum:
        raise RuntimeError(f"{name} must be between {minimum} and {maximum}")
    return value


def database_options(url):
    if url.startswith("sqlite"):
        return {
            "pool_pre_ping": True,
            "hide_parameters": True,
            "connect_args": {"check_same_thread": False, "timeout": 5},
        }
    parsed = make_url(url)
    # Keep schema-selection options used by isolated migration tests.
    options = str(parsed.query.get("options", ""))
    options += " -c timezone=UTC"
    for key, env, default in (
        ("statement_timeout", "SENTINEL_DB_STATEMENT_MS", 10000),
        ("lock_timeout", "SENTINEL_DB_LOCK_MS", 2000),
        ("idle_in_transaction_session_timeout", "SENTINEL_DB_IDLE_MS", 15000),
    ):
        options += f" -c {key}={integer(env, default, maximum=120000)}"
    return {
        "pool_pre_ping": True,
        "hide_parameters": True,
        "pool_size": integer("SENTINEL_DB_POOL_SIZE", 10, maximum=100),
        "max_overflow": integer("SENTINEL_DB_OVERFLOW", 0, minimum=0, maximum=100),
        "pool_timeout": integer("SENTINEL_DB_POOL_TIMEOUT_SECONDS", 3, maximum=30),
        "pool_recycle": 300,
        "pool_use_lifo": True,
        "connect_args": {"connect_timeout": 3, "options": options},
    }


def dispatch_deadline():
    return datetime.now(timezone.utc) + timedelta(
        seconds=integer("SENTINEL_APPROVAL_TTL_SECONDS", 300, minimum=30, maximum=3600)
    )

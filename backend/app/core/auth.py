"""
API Authentication
=====================
Simple API-key auth: every request must carry a matching
X-API-Key header, checked against SENTINEL_API_KEY from the
environment. This is deliberately simple (no OAuth, no user
accounts) -- appropriate for a service-to-service API where the
callers are the dashboard and the purchasing agent, not end users
signing in. A payments trust/verification layer with a wide-open,
unauthenticated API would undercut its own premise, so this exists
even though a hackathon demo could technically run without it.

If SENTINEL_API_KEY is unset, a dev-mode default is used and a loud
warning is logged -- the app still runs (so a fresh clone isn't
blocked), but this must never be left unset in anything resembling
a real deployment.
"""

from __future__ import annotations
import os
import logging
from fastapi import Header, HTTPException

logger = logging.getLogger("sentinel.auth")

DEV_DEFAULT_KEY = "dev-only-insecure-key-change-me"


def _configured_key() -> str:
    key = os.environ.get("SENTINEL_API_KEY", "")
    if not key:
        logger.warning(
            "SENTINEL_API_KEY is not set -- falling back to an insecure dev "
            "default. Set a real key in backend/.env before any real deployment."
        )
        return DEV_DEFAULT_KEY
    return key


async def require_api_key(x_api_key: str = Header(default="")):
    expected = _configured_key()
    if x_api_key != expected:
        raise HTTPException(status_code=401, detail="Missing or invalid X-API-Key header")

"""Scoped service credentials and revocable HttpOnly browser sessions.

SENTINEL_CREDENTIALS is a JSON list of {key, subject, role, owner_id, agent_ids}.
Roles: admin, owner, reviewer, agent. Legacy shared key is development-only.
"""

from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import hmac
import json
import os
from fastapi import Request, HTTPException
from app.models.db import SessionLocal, BrowserSession

DEV_DEFAULT_KEY = "dev-only-insecure-key-change-me"


@dataclass(frozen=True)
class Principal:
    subject: str
    role: str
    owner_id: str = ""
    agent_ids: tuple = ()

    def require(self, *roles):
        if self.role not in roles:
            raise HTTPException(403, "Insufficient permissions")

    def permits_agent(self, agent_id, db):
        if self.role == "admin":
            return True
        if self.role == "agent":
            return agent_id in self.agent_ids
        from app.models.db import PolicyRecord

        return (
            db.query(PolicyRecord)
            .filter_by(agent_id=agent_id, owner_human_id=self.owner_id)
            .first()
            is not None
        )


def credentials():
    configured = json.loads(os.environ.get("SENTINEL_CREDENTIALS", "[]"))
    if configured:
        if len({r.get("subject") for r in configured}) != len(configured) or len(
            {r.get("key") for r in configured}
        ) != len(configured):
            raise RuntimeError("Credential subjects and keys must be unique")
        for row in configured:
            if (
                not row.get("subject")
                or not isinstance(row.get("key"), str)
                or len(row.get("key", "")) < 32
                or not row["key"].isascii()
                or row.get("role") not in ("admin", "owner", "reviewer", "agent")
            ):
                raise RuntimeError("Invalid SENTINEL_CREDENTIALS configuration")
            if row["role"] in ("owner", "reviewer") and not row.get("owner_id"):
                raise RuntimeError("Owner scope required")
            if row["role"] == "agent" and not row.get("agent_ids"):
                raise RuntimeError("Agent scope required")
        return configured
    if os.environ.get("SENTINEL_ENV", "development") == "production":
        raise RuntimeError("Production requires scoped SENTINEL_CREDENTIALS")
    return [
        {
            "key": os.environ.get("SENTINEL_API_KEY") or DEV_DEFAULT_KEY,
            "subject": "local-admin",
            "role": "admin",
        }
    ]


def authenticate(key):
    if not isinstance(key, str) or not key.isascii() or not 1 <= len(key) <= 512:
        raise HTTPException(401, "Missing or invalid credential")
    for item in credentials():
        if hmac.compare_digest(key, item["key"]):
            return Principal(
                item["subject"],
                item["role"],
                item.get("owner_id", ""),
                tuple(item.get("agent_ids", [])),
            )
    raise HTTPException(401, "Missing or invalid credential")


def digest(value):
    return hashlib.sha256(value.encode()).hexdigest()


def require_api_key(request: Request):
    key = request.headers.get("X-API-Key", "")
    if key:
        principal = authenticate(key)
    else:
        token = request.cookies.get("sentinel_session", "")
        db = SessionLocal()
        try:
            session = db.get(BrowserSession, digest(token)) if token else None
            if not session or session.expires_at.replace(
                tzinfo=timezone.utc
            ) <= datetime.now(timezone.utc):
                raise HTTPException(401, "Sign in required")
            principal = None
            for item in credentials():
                if hmac.compare_digest(digest(item["key"]), session.credential_hash):
                    principal = authenticate(item["key"])
                    break
            if principal is None:
                raise HTTPException(401, "Credential revoked")
        finally:
            db.close()
        if request.method not in ("GET", "HEAD", "OPTIONS"):
            origin = request.headers.get("Origin")
            allowed = os.environ.get(
                "SENTINEL_ALLOWED_ORIGINS",
                "http://localhost:5173,http://127.0.0.1:5173",
            ).split(",")
            if origin not in allowed or request.headers.get("X-Sentinel-CSRF") != "1":
                raise HTTPException(403, "Invalid browser origin or CSRF header")
    request.state.principal = principal
    return principal


def require_agent(principal, agent_id, db):
    if not principal.permits_agent(agent_id, db):
        raise HTTPException(403, "Agent outside credential scope")

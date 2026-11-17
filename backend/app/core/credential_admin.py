"""Explicit shared credential provisioning; configuration never resurrects revoked keys."""

from datetime import timezone
import os
import secrets
from fastapi import HTTPException
from app.core.auth import credentials, digest, assert_current_principal
from app.core.intelligence import control_event, processing_time
from app.core.locking import agent_transaction
from app.models.db import ServiceCredential

AUTH_LOCK = "__sentinel_credentials_v1__"


def metadata(row):
    return {
        "subject": row.subject,
        "role": row.role,
        "owner_id": row.owner_id,
        "agent_ids": row.agent_ids,
        "active": row.active,
        "version": row.version,
        "expires_at": row.expires_at.replace(tzinfo=timezone.utc).isoformat()
        if row.expires_at
        else None,
    }


def bootstrap(db):
    with agent_transaction(db, AUTH_LOCK):
        if db.query(ServiceCredential).count():
            return False
        if (
            not os.environ.get("SENTINEL_CREDENTIALS")
            or os.environ["SENTINEL_CREDENTIALS"] == "[]"
        ):
            raise RuntimeError(
                "Shared authority bootstrap requires explicit scoped credentials"
            )
        entries = credentials()
        if not any(r["role"] == "admin" for r in entries):
            raise RuntimeError("Bootstrap requires an administrator")
        for row in entries:
            db.add(
                ServiceCredential(
                    subject=row["subject"],
                    key_hash=digest(row["key"]),
                    role=row["role"],
                    owner_id=row.get("owner_id", ""),
                    agent_ids=row.get("agent_ids", []),
                    active=True,
                    version=1,
                    updated_by="bootstrap",
                )
            )
        control_event(
            db,
            "privileged-operator",
            "credential_bootstrap",
            "registry",
            None,
            {"subjects": [r["subject"] for r in entries]},
        )
    return True


def change(db, actor, body):
    key = None
    with agent_transaction(db, AUTH_LOCK):
        assert_current_principal(actor, db)
        row = (
            db.query(ServiceCredential)
            .filter_by(subject=body.subject)
            .populate_existing()
            .with_for_update()
            .first()
        )
        if body.action == "create":
            if row is not None:
                raise HTTPException(
                    409, "Subject already exists; rotate or change scope explicitly"
                )
            if body.expected_version != 0:
                raise HTTPException(409, "New credentials require expected_version=0")
            row = ServiceCredential(
                subject=body.subject,
                key_hash="",
                role=body.role,
                owner_id=body.owner_id,
                agent_ids=body.agent_ids,
                active=True,
                version=0,
                updated_by=actor.subject,
            )
            before = None
            db.add(row)
        else:
            if row is None:
                raise HTTPException(404, "Unknown subject")
            if row.version != body.expected_version:
                raise HTTPException(
                    409, "Credential changed; refresh metadata before retrying"
                )
            before = metadata(row)
        if (
            body.action == "revoke"
            or (body.action == "scope" and body.role != "admin")
            or (body.action == "rotate" and body.expires_at is not None)
        ):
            if row.active and row.role == "admin":
                now = processing_time(db)
                others = (
                    db.query(ServiceCredential)
                    .filter(
                        ServiceCredential.subject != row.subject,
                        ServiceCredential.role == "admin",
                        ServiceCredential.active.is_(True),
                    )
                    .all()
                )
                if not any(
                    r.expires_at is None
                    or r.expires_at.replace(tzinfo=timezone.utc) > now
                    for r in others
                ):
                    raise HTTPException(
                        409, "Cannot remove the last active administrator"
                    )
        if body.action in ("create", "rotate"):
            key = secrets.token_urlsafe(32)
            row.key_hash = digest(key)
            row.active = True
            row.expires_at = body.expires_at
        if body.action == "revoke":
            row.active = False
        if body.action == "scope":
            row.role, row.owner_id, row.agent_ids = (
                body.role,
                body.owner_id,
                body.agent_ids,
            )
        row.version += 1
        row.updated_by, row.updated_at = actor.subject, processing_time(db)
        after = metadata(row)
        control_event(
            db, actor.subject, "credential_" + body.action, row.subject, before, after
        )
    return {
        "principal": after,
        "credential": key,
        "note": "Store a new credential securely now; it is not returned again"
        if key
        else "Shared authority updated",
    }

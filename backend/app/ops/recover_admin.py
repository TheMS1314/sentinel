"""Privileged recovery writes a new administrator key to a new 0600 file, never stdout."""

from app.core.env import load_env

load_env()
import os
import re
import secrets
from pathlib import Path
from app.core.auth import digest
from app.core.credential_admin import AUTH_LOCK, metadata
from app.core.intelligence import control_event, processing_time
from app.core.locking import agent_transaction
from app.models.db import ServiceCredential, SessionLocal


def recover(db, subject, reason, output):
    if (
        not re.fullmatch(r"[A-Za-z0-9_.:-]{1,128}", subject)
        or not 1 <= len(reason.strip()) <= 2000
    ):
        raise ValueError("Provide a valid subject and incident reason")
    key = secrets.token_urlsafe(32)
    fd = os.open(output, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(fd, "w") as handle:
            handle.write(key + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        with agent_transaction(db, AUTH_LOCK):
            row = (
                db.query(ServiceCredential)
                .filter_by(subject=subject)
                .populate_existing()
                .with_for_update()
                .first()
            )
            before = metadata(row) if row else None
            if row is None:
                row = ServiceCredential(subject=subject, version=0)
                db.add(row)
            row.key_hash, row.role, row.active = digest(key), "admin", True
            row.owner_id, row.agent_ids, row.expires_at = "", [], None
            row.version += 1
            row.updated_by, row.updated_at = "privileged-operator", processing_time(db)
            control_event(
                db,
                "privileged-operator",
                "admin_recovery",
                subject,
                before,
                dict(metadata(row), reason=reason.strip()),
            )
    except BaseException:
        Path(output).unlink(missing_ok=True)
        raise


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser()
    parser.add_argument("--subject", required=True)
    parser.add_argument("--reason", required=True)
    parser.add_argument("--output", required=True)
    args = parser.parse_args()
    with SessionLocal() as db:
        recover(db, args.subject, args.reason, args.output)
    print(
        "Administrator recovery recorded; retrieve the credential from the protected output file and move it to your secret manager"
    )

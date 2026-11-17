"""Emergency freeze through PostgreSQL when Redis/API ingress is unavailable.

This only freezes. Resume through the versioned administrator control after
reviewing pending payments. Shell/database access is a privileged trust boundary.
"""

from app.core.env import load_env

load_env()
from app.core.intelligence import GUARD_LOCK, control_event, guard_view, processing_time
from app.core.locking import agent_transaction
from app.models.db import SessionLocal, DispatchGuard


def freeze(db, reason):
    if not 1 <= len(reason.strip()) <= 2000:
        raise ValueError("Provide an incident reason of 1..2000 characters")
    with agent_transaction(db, GUARD_LOCK):
        row = db.query(DispatchGuard).filter_by(id=1).populate_existing().first()
        before = guard_view(row)
        if row is None:
            row = DispatchGuard(id=1, window_seconds=60)
            db.add(row)
        row.frozen, row.version = True, before["version"] + 1
        row.reason, row.updated_by, row.updated_at = (
            reason.strip(),
            "privileged-operator",
            processing_time(db),
        )
        after = guard_view(row)
        control_event(db, row.updated_by, "emergency_freeze", "global", before, after)
    return after


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser()
    parser.add_argument("--reason", required=True)
    args = parser.parse_args()
    with SessionLocal() as db:
        result = freeze(db, args.reason)
    print("Dispatch frozen; guard version " + str(result["version"]))

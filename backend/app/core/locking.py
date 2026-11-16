"""Per-agent cross-replica transaction lock, including first policy creation."""

from contextlib import contextmanager, nullcontext
from hashlib import sha256
from threading import RLock
from sqlalchemy import text

_local = RLock()  # SQLite is used only by isolated unit tests.


@contextmanager
def agent_transaction(db, agent_id):
    postgres = db.bind.dialect.name == "postgresql"
    with nullcontext() if postgres else _local:
        try:
            if postgres:
                key = int.from_bytes(
                    sha256(agent_id.encode()).digest()[:8], "big", signed=True
                )
                db.execute(text("SELECT pg_advisory_xact_lock(:key)"), {"key": key})
            yield
            db.commit()
        except BaseException:
            db.rollback()
            raise

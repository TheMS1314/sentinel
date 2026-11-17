"""Run migrations and one-time shared-authority bootstrap with deployment access."""

from pathlib import Path
import os
from app.core.env import load_env

load_env()

if __name__ == "__main__":
    if os.environ.get("SENTINEL_MIGRATION_DATABASE_URL"):
        os.environ["SENTINEL_DATABASE_URL"] = os.environ.pop(
            "SENTINEL_MIGRATION_DATABASE_URL"
        )
    from alembic import command
    from alembic.config import Config
    from app.core.auth import database_authority
    from app.core.credential_admin import bootstrap
    from app.models.db import SessionLocal

    command.upgrade(
        Config(str(Path(__file__).resolve().parents[2] / "alembic.ini")), "head"
    )
    if database_authority():
        with SessionLocal() as db:
            initialized = bootstrap(db)
        print(
            "Shared authority initialized"
            if initialized
            else "Shared authority already exists; configuration did not overwrite it"
        )
    if os.environ.get("SENTINEL_RUNTIME_DB_PASSWORD"):
        from app.ops.db_privileges import provision
        from app.models.db import engine

        provision(
            engine,
            os.environ.get("SENTINEL_RUNTIME_DB_ROLE", "sentinel_runtime"),
            os.environ["SENTINEL_RUNTIME_DB_PASSWORD"],
        )
    print("Schema initialization complete")

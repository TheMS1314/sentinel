"""Run migrations and one-time shared-authority bootstrap with deployment access."""

from pathlib import Path
from app.core.env import load_env

load_env()

if __name__ == "__main__":
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
    print("Schema initialization complete")

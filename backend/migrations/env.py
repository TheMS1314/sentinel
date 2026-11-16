from alembic import context
from app.core.env import load_env

load_env()
from app.models.db import engine, Base

config = context.config


def online():
    with engine.connect() as conn:
        context.configure(
            connection=conn, target_metadata=Base.metadata, compare_type=True
        )
        with context.begin_transaction():
            context.run_migrations()


online()

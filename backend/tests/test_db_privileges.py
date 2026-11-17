"""Prove restricted runtime authority with PostgreSQL, not a SQL string snapshot."""

import secrets
import uuid
import pytest
import sqlalchemy as sa
from app.models.db import engine, Base
from app.ops.db_privileges import provision, validate


def test_runtime_role_cannot_rewrite_or_erase_audit_or_create_schema_objects():
    if engine.dialect.name != "postgresql":
        pytest.skip("Dedicated PostgreSQL test database is required")
    Base.metadata.create_all(engine)
    # Alembic metadata is separate from ORM metadata in the normal test database.
    with engine.begin() as c:
        c.exec_driver_sql(
            "CREATE TABLE IF NOT EXISTS alembic_version(version_num VARCHAR PRIMARY KEY)"
        )
    role = "runtime_test_" + uuid.uuid4().hex[:12]
    password = secrets.token_urlsafe(32)
    runtime = None
    try:
        provision(engine, role, password)
        runtime = sa.create_engine(
            engine.url.set(username=role, password=password), hide_parameters=True
        )
        from sqlalchemy.orm import Session

        with Session(runtime) as db:
            validate(db)
        for statement in (
            "CREATE TABLE public.forbidden_runtime_table(id INT)",
            "DELETE FROM public.transactions WHERE false",
            "TRUNCATE public.risk_control_events",
            "UPDATE public.decisions SET verdict='APPROVE' WHERE false",
            "UPDATE public.payments SET amount_paise=1 WHERE false",
        ):
            with runtime.connect() as c:
                with pytest.raises(sa.exc.DBAPIError):
                    c.exec_driver_sql(statement)
        with runtime.begin() as c:
            c.exec_driver_sql(
                "UPDATE public.decisions SET razorpay_status='not_sent' WHERE false"
            )
            c.exec_driver_sql("DELETE FROM public.evaluation_jobs WHERE false")
            c.exec_driver_sql("SELECT * FROM public.service_credentials LIMIT 1")
    finally:
        if runtime is not None:
            runtime.dispose()
        with engine.begin() as c:
            c.exec_driver_sql('DROP OWNED BY "' + role + '"')
            c.exec_driver_sql('DROP ROLE "' + role + '"')

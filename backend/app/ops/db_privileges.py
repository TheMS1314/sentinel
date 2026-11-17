"""Provision reviewed grants using a deployment-only connection, never the HTTP API."""

import os
from psycopg2 import sql
from sqlalchemy import text

IMMUTABLE = (
    "transactions",
    "review_events",
    "provider_events",
    "intelligence_assessments",
    "dispatch_claims",
    "risk_control_events",
    "provider_evidence",
)
MUTABLE = (
    "merchant_risk_profiles",
    "threat_indicators",
    "dispatch_guard",
    "evaluation_jobs",
    "service_credentials",
    "merchant_identities",
    "incidents",
    "alert_deliveries",
    "provider_bindings",
)
LIMITED = {
    "policies": ("active",),
    "decisions": (
        "razorpay_status",
        "razorpay_ref",
        "human_override",
        "resolved_by",
        "resolved_at",
        "resolution_reason",
    ),
    "payments": (
        "state",
        "budget_day",
        "order_id",
        "error",
        "updated_at",
        "dispatch_expires_at",
        "authorization_policy_id",
        "authorizing_credential_version",
        "authorized_merchant_version",
    ),
}
TABLES = IMMUTABLE + MUTABLE + tuple(LIMITED) + ("browser_sessions",)


def provision(engine, role, password):
    if not password or len(password) < 32:
        raise ValueError("Runtime role needs a 32+ character password")
    connection = engine.raw_connection()
    try:
        with connection.cursor() as cursor:
            cursor.execute("SELECT 1 FROM pg_roles WHERE rolname=%s", (role,))
            if cursor.fetchone() is None:
                cursor.execute(
                    sql.SQL("CREATE ROLE {} LOGIN").format(sql.Identifier(role))
                )
            cursor.execute(
                "SELECT 1 FROM pg_auth_members m JOIN pg_roles r ON r.oid=m.member WHERE r.rolname=%s",
                (role,),
            )
            if cursor.fetchone():
                raise ValueError("Runtime role must not belong to other database roles")
            cursor.execute(
                "SELECT 1 FROM pg_class c JOIN pg_roles r ON r.oid=c.relowner WHERE r.rolname=%s",
                (role,),
            )
            if cursor.fetchone():
                raise ValueError("Runtime role must not own database objects")
            cursor.execute(
                sql.SQL(
                    "ALTER ROLE {} WITH NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT NOBYPASSRLS PASSWORD %s"
                ).format(sql.Identifier(role)),
                (password,),
            )
            cursor.execute("REVOKE CREATE ON SCHEMA public FROM PUBLIC")
            cursor.execute(
                sql.SQL("REVOKE ALL ON SCHEMA public FROM {}").format(
                    sql.Identifier(role)
                )
            )
            cursor.execute(
                sql.SQL("GRANT USAGE ON SCHEMA public TO {}").format(
                    sql.Identifier(role)
                )
            )
            cursor.execute(
                sql.SQL("REVOKE ALL ON ALL TABLES IN SCHEMA public FROM {}").format(
                    sql.Identifier(role)
                )
            )
            # Table-level REVOKE does not erase old column grants. Clear those too.
            cursor.execute(
                "SELECT table_name,column_name FROM information_schema.columns WHERE table_schema='public'"
            )
            for table, column in cursor.fetchall():
                cursor.execute(
                    sql.SQL("REVOKE ALL ({}) ON TABLE public.{} FROM {}").format(
                        sql.Identifier(column),
                        sql.Identifier(table),
                        sql.Identifier(role),
                    )
                )
            for table in TABLES:
                cursor.execute(
                    sql.SQL("GRANT SELECT, INSERT ON TABLE public.{} TO {}").format(
                        sql.Identifier(table), sql.Identifier(role)
                    )
                )
            for table in MUTABLE:
                cursor.execute(
                    sql.SQL("GRANT UPDATE ON TABLE public.{} TO {}").format(
                        sql.Identifier(table), sql.Identifier(role)
                    )
                )
            for table, columns in LIMITED.items():
                cursor.execute(
                    sql.SQL("GRANT UPDATE ({}) ON TABLE public.{} TO {}").format(
                        sql.SQL(",").join(map(sql.Identifier, columns)),
                        sql.Identifier(table),
                        sql.Identifier(role),
                    )
                )
            for table in ("evaluation_jobs", "browser_sessions"):
                cursor.execute(
                    sql.SQL("GRANT DELETE ON TABLE public.{} TO {}").format(
                        sql.Identifier(table), sql.Identifier(role)
                    )
                )
            cursor.execute(
                sql.SQL("GRANT SELECT ON public.alembic_version TO {}").format(
                    sql.Identifier(role)
                )
            )
            cursor.execute(
                sql.SQL(
                    "GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO {}"
                ).format(sql.Identifier(role))
            )
        connection.commit()
    except BaseException:
        connection.rollback()
        raise
    finally:
        connection.close()


def validate(db):
    """Reject owner/superuser, schema creation and dangerous historical writes."""
    if db.bind.dialect.name != "postgresql":
        raise RuntimeError("Restricted runtime authority requires PostgreSQL")
    elevated = db.execute(
        text(
            "SELECT rolsuper OR rolcreatedb OR rolcreaterole OR rolbypassrls FROM pg_roles WHERE rolname=current_user"
        )
    ).scalar_one()
    writable = db.execute(
        text(
            "SELECT has_schema_privilege(current_user,'public','CREATE') OR has_column_privilege(current_user,'public.decisions','verdict','UPDATE') OR has_column_privilege(current_user,'public.payments','amount_paise','UPDATE')"
        )
    ).scalar_one()
    erasable = any(
        db.execute(
            text(
                "SELECT has_table_privilege(current_user,:table,'DELETE') OR has_table_privilege(current_user,:table,'TRUNCATE')"
            ),
            {"table": "public." + table},
        ).scalar_one()
        for table in IMMUTABLE + ("decisions", "payments", "policies")
    )
    if elevated or writable or erasable:
        raise RuntimeError(
            "Runtime database role has excessive privileges; use the provisioned restricted role"
        )


if __name__ == "__main__":
    from app.core.env import load_env

    load_env()
    from app.models.db import engine

    provision(
        engine,
        os.environ.get("SENTINEL_RUNTIME_DB_ROLE", "sentinel_runtime"),
        os.environ.get("SENTINEL_RUNTIME_DB_PASSWORD", ""),
    )
    print("Runtime database grants applied; no credential was printed")

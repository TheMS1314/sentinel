"""Run real Alembic upgrades against disposable SQLite and PostgreSQL schemas."""

import os
from pathlib import Path
import subprocess
import sys
import uuid

import pytest
import sqlalchemy as sa

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(params=["sqlite", "postgresql"])
def migration_database(request, tmp_path):
    admin = None
    schema = None
    if request.param == "postgresql":
        raw = os.environ.get("SENTINEL_TEST_DATABASE_URL", "")
        if not raw.startswith("postgresql"):
            pytest.skip("Dedicated PostgreSQL test database is not configured")
        url = sa.engine.make_url(raw)
        assert url.database.endswith("_test")
        admin = sa.create_engine(url)
        schema = "migration_" + uuid.uuid4().hex
        with admin.begin() as conn:
            conn.exec_driver_sql(f'CREATE SCHEMA "{schema}"')
        url = url.update_query_dict({"options": "-csearch_path=" + schema})
    else:
        url = sa.engine.make_url("sqlite:///" + str(tmp_path / "migration.sqlite"))
    engine = sa.create_engine(url)
    try:
        yield url.render_as_string(hide_password=False), engine
    finally:
        engine.dispose()
        if admin is not None:
            with admin.begin() as conn:
                conn.exec_driver_sql(f'DROP SCHEMA "{schema}" CASCADE')
            admin.dispose()


def upgrade(url):
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "alembic",
            "-c",
            str(ROOT / "alembic.ini"),
            "upgrade",
            "head",
        ],
        env=dict(os.environ, SENTINEL_DATABASE_URL=url),
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr


def test_adopts_legacy_history_without_executing_payments(migration_database):
    url, engine = migration_database
    ddl = """
      CREATE TABLE policies (id INTEGER PRIMARY KEY, policy_id VARCHAR UNIQUE NOT NULL,
        agent_id VARCHAR NOT NULL, owner_human_id VARCHAR NOT NULL, version INTEGER,
        active BOOLEAN, policy_json JSON NOT NULL, created_at DATETIME);
      CREATE TABLE transactions (id INTEGER PRIMARY KEY, request_id VARCHAR UNIQUE NOT NULL,
        agent_id VARCHAR NOT NULL, merchant_name VARCHAR NOT NULL, merchant_category VARCHAR NOT NULL,
        amount FLOAT NOT NULL, requested_at_hour INTEGER NOT NULL, requested_at_iso VARCHAR NOT NULL,
        stated_intent TEXT NOT NULL, raw_agent_goal TEXT, scenario_tag VARCHAR, created_at DATETIME);
      CREATE TABLE decisions (id INTEGER PRIMARY KEY, request_id VARCHAR UNIQUE NOT NULL REFERENCES transactions(request_id),
        verdict VARCHAR NOT NULL, reasons JSON NOT NULL, checked_rules JSON NOT NULL, decided_by VARCHAR NOT NULL,
        similar_past_cases JSON, confidence FLOAT, model_version VARCHAR, razorpay_status VARCHAR,
        razorpay_ref VARCHAR, human_override VARCHAR, created_at DATETIME);
      INSERT INTO policies VALUES (1,'p1','a','owner',1,1,'{}',NULL);
      INSERT INTO transactions VALUES (1,'r1','a','m','grocery',29.29,14,'2026-01-01T14:00:00','buy groceries','buy groceries','',NULL);
      INSERT INTO decisions VALUES (1,'r1','APPROVE','[]','[]','rule_engine','[]',NULL,NULL,'created','order1',NULL,NULL);
    """
    if engine.dialect.name == "postgresql":
        ddl = ddl.replace("DATETIME", "TIMESTAMP").replace(
            "'owner',1,1,", "'owner',1,true,"
        )
    with engine.begin() as conn:
        for statement in ddl.split(";"):
            if statement.strip():
                conn.exec_driver_sql(statement)
    upgrade(url)
    with engine.connect() as conn:
        assert conn.execute(
            sa.text("SELECT amount_paise,state FROM payments")
        ).one() == (2929, "unknown")
        assert (
            conn.execute(sa.text("SELECT COUNT(*) FROM transactions")).scalar_one() == 1
        )
        assert not conn.execute(sa.text("SELECT active FROM policies")).scalar_one()
    assert "policy_snapshot" in {
        c["name"] for c in sa.inspect(engine).get_columns("decisions")
    }
    if engine.dialect.name == "postgresql":
        assert isinstance(
            sa.inspect(engine).get_columns("transactions")[5]["type"], sa.Numeric
        )
    upgrade(url)


def test_creates_fresh_schema(migration_database):
    url, engine = migration_database
    upgrade(url)
    assert {
        "policies",
        "transactions",
        "decisions",
        "payments",
        "review_events",
        "browser_sessions",
        "merchant_risk_profiles",
        "threat_indicators",
        "intelligence_assessments",
        "dispatch_guard",
        "dispatch_claims",
        "risk_control_events",
    }.issubset(sa.inspect(engine).get_table_names())
    upgrade(url)


def test_upgrades_already_hardened_schema(migration_database):
    url, engine = migration_database
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "alembic",
            "-c",
            str(ROOT / "alembic.ini"),
            "upgrade",
            "0001_hardened",
        ],
        env=dict(os.environ, SENTINEL_DATABASE_URL=url),
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
    assert "dispatch_guard" not in sa.inspect(engine).get_table_names()
    upgrade(url)
    assert "dispatch_guard" in sa.inspect(engine).get_table_names()
    with engine.connect() as conn:
        assert (
            conn.execute(
                sa.text("SELECT version_num FROM alembic_version")
            ).scalar_one()
            == "0003_runtime_safety"
        )

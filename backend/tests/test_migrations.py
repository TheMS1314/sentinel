"""Run real Alembic upgrades against disposable SQLite and PostgreSQL schemas."""

import os
from pathlib import Path
import subprocess
import sys
import uuid

import pytest
import sqlalchemy as sa

from app.schema_version import SCHEMA_REVISION

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
            == SCHEMA_REVISION
        )


def test_upgrade_preserves_existing_provider_evidence_and_keeps_refresh_opt_in(
    migration_database,
):
    url, engine = migration_database
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "alembic",
            "-c",
            str(ROOT / "alembic.ini"),
            "upgrade",
            "0007_provider_evidence",
        ],
        env=dict(os.environ, SENTINEL_DATABASE_URL=url),
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
    with engine.begin() as c:
        c.execute(
            sa.text(
                "INSERT INTO merchant_identities(merchant_id,owner_id,legal_name,category,domain,operator_reference,active,version,approved_by,updated_at) VALUES ('m','o','Shop','grocery','shop.example','operator',true,1,'admin',CURRENT_TIMESTAMP)"
            )
        )
        c.execute(
            sa.text(
                "INSERT INTO provider_bindings(binding_id,merchant_id,provider,external_id,version,updated_by,updated_at) VALUES ('b','m','recorded_future','idn:shop.example',1,'admin',CURRENT_TIMESTAMP)"
            )
        )
        c.execute(
            sa.text(
                "INSERT INTO provider_evidence(evidence_id,binding_id,binding_version,merchant_version,status,score,observed_at,fetched_at,expires_at,response_hash) VALUES ('e','b',1,1,'current',80,CURRENT_TIMESTAMP,CURRENT_TIMESTAMP,CURRENT_TIMESTAMP,'retained-hash')"
            )
        )
    upgrade(url)
    with engine.connect() as c:
        row = c.execute(
            sa.text(
                "SELECT auto_refresh,refresh_generation,refresh_token,merchant_version FROM provider_bindings"
            )
        ).one()
        assert not row[0] and row[1] == 0 and row[2] is None and row[3] == 0
        evidence = c.execute(
            sa.text(
                "SELECT evidence_id,score,response_hash,refresh_generation FROM provider_evidence"
            )
        ).one()
        assert evidence == ("e", 80, "retained-hash", 0)

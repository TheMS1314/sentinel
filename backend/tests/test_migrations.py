"""Migration checks run in a separate process against disposable databases."""

import os
from pathlib import Path
import sqlite3
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]


def test_adopts_legacy_history_without_executing_payments(tmp_path):
    path = tmp_path / "legacy.sqlite"
    conn = sqlite3.connect(path)
    conn.executescript("""
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
    """)
    conn.commit()
    conn.close()
    env = dict(os.environ, SENTINEL_DATABASE_URL="sqlite:///" + str(path))
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
        env=env,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
    conn = sqlite3.connect(path)
    assert conn.execute("SELECT amount_paise,state FROM payments").fetchone() == (
        2929,
        "unknown",
    )
    assert conn.execute("SELECT COUNT(*) FROM transactions").fetchone()[0] == 1
    assert "policy_snapshot" in [
        r[1] for r in conn.execute("PRAGMA table_info(decisions)")
    ]
    conn.close()
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
        env=env,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr

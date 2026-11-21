"""Bound pending authorization lifetime and index recovery/retention paths."""

from alembic import op
import sqlalchemy as sa

revision = "0003_runtime_safety"
down_revision = "0002_risk_intelligence"
branch_labels = depends_on = None


def upgrade():
    bind = op.get_bind()
    if "dispatch_expires_at" not in {
        c["name"] for c in sa.inspect(bind).get_columns("payments")
    }:
        op.add_column(
            "payments", sa.Column("dispatch_expires_at", sa.DateTime(), nullable=True)
        )
    if "authorization_policy_id" not in {
        c["name"] for c in sa.inspect(bind).get_columns("payments")
    }:
        op.add_column(
            "payments", sa.Column("authorization_policy_id", sa.String(), nullable=True)
        )
    if "submitted_by" not in {
        c["name"] for c in sa.inspect(bind).get_columns("transactions")
    }:
        op.add_column(
            "transactions",
            sa.Column(
                "submitted_by", sa.String(), nullable=False, server_default="legacy"
            ),
        )
    # An old pending decision has no bounded validity contract. Expire before
    # dispatch rather than silently granting another five minutes of authority.
    bind.execute(
        sa.text(
            "UPDATE payments SET dispatch_expires_at=CURRENT_TIMESTAMP WHERE state='pending' AND dispatch_expires_at IS NULL"
        )
    )
    if "evaluation_jobs" not in sa.inspect(bind).get_table_names():
        op.create_table(
            "evaluation_jobs",
            sa.Column("job_id", sa.String(), primary_key=True),
            sa.Column("batch_id", sa.String()),
            sa.Column("submitted_by", sa.String(), nullable=False),
            sa.Column("agent_id", sa.String(), nullable=False),
            sa.Column("payload", sa.JSON(), nullable=False),
            sa.Column("status", sa.String(), nullable=False),
            sa.Column("result", sa.JSON()),
            sa.Column("error", sa.String()),
            sa.Column("attempts", sa.Integer(), nullable=False),
            sa.Column("lease_token", sa.String()),
            sa.Column("lease_expires_at", sa.DateTime()),
            sa.Column("created_at", sa.DateTime(), nullable=False),
            sa.Column("updated_at", sa.DateTime(), nullable=False),
            sa.Column("completed_at", sa.DateTime()),
        )
        op.create_index(
            "ix_job_claim",
            "evaluation_jobs",
            ["status", "lease_expires_at", "created_at"],
        )
        op.create_index("ix_job_batch", "evaluation_jobs", ["batch_id", "created_at"])
        op.create_index("ix_job_retention", "evaluation_jobs", ["completed_at"])
    specs = [
        ("ix_payment_recovery", "payments", ["state", "updated_at"]),
        ("ix_transaction_processing", "transactions", ["agent_id", "created_at"]),
        ("ix_session_expiry", "browser_sessions", ["expires_at"]),
    ]
    for name, table, cols in specs:
        if name not in {i["name"] for i in sa.inspect(bind).get_indexes(table)}:
            op.create_index(name, table, cols)


def downgrade():
    raise RuntimeError(
        "Destructive downgrade disabled; restore a reviewed backup instead"
    )

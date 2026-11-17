"""Durable operational incidents and signed alert delivery."""

from alembic import op
import sqlalchemy as sa

revision = "0006_operations"
down_revision = "0005_merchant_identity"
branch_labels = depends_on = None


def upgrade():
    bind = op.get_bind()
    if "incidents" not in sa.inspect(bind).get_table_names():
        op.create_table(
            "incidents",
            sa.Column("incident_id", sa.String(), primary_key=True),
            sa.Column("fingerprint", sa.String(), nullable=False),
            sa.Column("kind", sa.String(), nullable=False),
            sa.Column("entity_id", sa.String(), nullable=False),
            sa.Column("severity", sa.String(), nullable=False),
            sa.Column("state", sa.String(), nullable=False),
            sa.Column("condition_active", sa.Boolean(), nullable=False),
            sa.Column("summary", sa.Text(), nullable=False),
            sa.Column("version", sa.Integer(), nullable=False),
            sa.Column("assigned_to", sa.String()),
            sa.Column("last_reason", sa.Text()),
            sa.Column("first_seen", sa.DateTime(), nullable=False),
            sa.Column("last_seen", sa.DateTime(), nullable=False),
        )
        op.create_index(
            "ix_incidents_fingerprint", "incidents", ["fingerprint"], unique=True
        )
    if "alert_deliveries" not in sa.inspect(bind).get_table_names():
        op.create_table(
            "alert_deliveries",
            sa.Column("event_id", sa.String(), primary_key=True),
            sa.Column(
                "incident_id",
                sa.String(),
                sa.ForeignKey("incidents.incident_id"),
                nullable=False,
            ),
            sa.Column("payload", sa.JSON(), nullable=False),
            sa.Column("state", sa.String(), nullable=False),
            sa.Column("attempts", sa.Integer(), nullable=False),
            sa.Column("next_attempt_at", sa.DateTime(), nullable=False),
            sa.Column("lease_token", sa.String()),
            sa.Column("lease_expires_at", sa.DateTime()),
            sa.Column("last_error", sa.String()),
            sa.Column("delivered_at", sa.DateTime()),
            sa.Column("created_at", sa.DateTime(), nullable=False),
        )
        op.create_index(
            "ix_alert_dispatch", "alert_deliveries", ["state", "next_attempt_at"]
        )


def downgrade():
    raise RuntimeError(
        "Destructive downgrade disabled; restore a reviewed backup instead"
    )

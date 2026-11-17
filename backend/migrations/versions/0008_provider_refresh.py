"""Opt-in scheduled provider refresh with durable leases and response fencing."""

from alembic import op
import sqlalchemy as sa

revision = "0008_provider_refresh"
down_revision = "0007_provider_evidence"
branch_labels = depends_on = None


def upgrade():
    op.add_column("incidents", sa.Column("last_checked_at", sa.DateTime()))
    op.create_index(
        "ix_incident_monitor_scan", "incidents", ["condition_active", "last_checked_at"]
    )
    for column in (
        sa.Column("merchant_version", sa.Integer(), nullable=False, server_default="0"),
        sa.Column(
            "auto_refresh", sa.Boolean(), nullable=False, server_default=sa.false()
        ),
        sa.Column(
            "refresh_interval_seconds",
            sa.Integer(),
            nullable=False,
            server_default="300",
        ),
        sa.Column(
            "refresh_next_at",
            sa.DateTime(),
            nullable=False,
            server_default=sa.text("'1970-01-01 00:00:00'"),
        ),
        sa.Column(
            "refresh_generation", sa.Integer(), nullable=False, server_default="0"
        ),
        sa.Column("refresh_token", sa.String()),
        sa.Column("refresh_lease_expires_at", sa.DateTime()),
        sa.Column(
            "refresh_status", sa.String(), nullable=False, server_default="never"
        ),
        sa.Column("refresh_error", sa.String()),
    ):
        op.add_column("provider_bindings", column)
    op.add_column(
        "provider_evidence",
        sa.Column(
            "refresh_generation", sa.Integer(), nullable=False, server_default="0"
        ),
    )
    op.create_index(
        "ix_provider_refresh_due",
        "provider_bindings",
        ["auto_refresh", "refresh_next_at"],
    )
    op.create_index(
        "ix_provider_evidence_generation",
        "provider_evidence",
        ["binding_id", "refresh_generation", "fetched_at"],
    )
    op.create_index(
        "ix_provider_source_watermark",
        "provider_evidence",
        ["binding_id", "binding_version", "merchant_version", "observed_at"],
    )


def downgrade():
    raise RuntimeError(
        "Destructive downgrade disabled; restore a reviewed backup instead"
    )

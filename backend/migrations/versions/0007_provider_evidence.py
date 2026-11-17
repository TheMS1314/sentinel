"""Versioned provider bindings and append-only, minimized enrichment evidence."""

from alembic import op
import sqlalchemy as sa

revision = "0007_provider_evidence"
down_revision = "0006_operations"
branch_labels = depends_on = None


def upgrade():
    op.create_table(
        "provider_bindings",
        sa.Column("binding_id", sa.String(), primary_key=True),
        sa.Column(
            "merchant_id",
            sa.String(),
            sa.ForeignKey("merchant_identities.merchant_id"),
            nullable=False,
        ),
        sa.Column("provider", sa.String(), nullable=False),
        sa.Column("external_id", sa.String(), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("updated_by", sa.String(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.UniqueConstraint("merchant_id", "provider", name="uq_provider_merchant"),
    )
    op.create_table(
        "provider_evidence",
        sa.Column("evidence_id", sa.String(), primary_key=True),
        sa.Column(
            "binding_id",
            sa.String(),
            sa.ForeignKey("provider_bindings.binding_id"),
            nullable=False,
        ),
        sa.Column("binding_version", sa.Integer(), nullable=False),
        sa.Column("merchant_version", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(), nullable=False),
        sa.Column("score", sa.Float()),
        sa.Column("observed_at", sa.DateTime()),
        sa.Column("fetched_at", sa.DateTime(), nullable=False),
        sa.Column("expires_at", sa.DateTime(), nullable=False),
        sa.Column("response_hash", sa.String()),
        sa.Column("error", sa.String()),
    )
    op.create_index(
        "ix_provider_evidence_lookup", "provider_evidence", ["binding_id", "fetched_at"]
    )


def downgrade():
    raise RuntimeError(
        "Destructive downgrade disabled; restore a reviewed backup instead"
    )

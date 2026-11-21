"""Shared service-credential authority. Secrets are hashed; bootstrap is explicit."""

from alembic import op
import sqlalchemy as sa

revision = "0004_shared_authority"
down_revision = "0003_runtime_safety"
branch_labels = depends_on = None


def upgrade():
    bind = op.get_bind()
    for table, name in (
        ("transactions", "submitted_credential_version"),
        ("payments", "authorizing_credential_version"),
    ):
        if name not in {c["name"] for c in sa.inspect(bind).get_columns(table)}:
            op.add_column(
                table, sa.Column(name, sa.Integer(), nullable=False, server_default="1")
            )
    if "service_credentials" not in sa.inspect(bind).get_table_names():
        op.create_table(
            "service_credentials",
            sa.Column("subject", sa.String(), primary_key=True),
            sa.Column("key_hash", sa.String(), nullable=False),
            sa.Column("role", sa.String(), nullable=False),
            sa.Column("owner_id", sa.String(), nullable=False),
            sa.Column("agent_ids", sa.JSON(), nullable=False),
            sa.Column("active", sa.Boolean(), nullable=False),
            sa.Column("expires_at", sa.DateTime()),
            sa.Column("version", sa.Integer(), nullable=False),
            sa.Column("updated_by", sa.String(), nullable=False),
            sa.Column("updated_at", sa.DateTime(), nullable=False),
        )
        op.create_index(
            "ix_service_credentials_key_hash",
            "service_credentials",
            ["key_hash"],
            unique=True,
        )


def downgrade():
    raise RuntimeError(
        "Destructive downgrade disabled; restore a reviewed backup instead"
    )

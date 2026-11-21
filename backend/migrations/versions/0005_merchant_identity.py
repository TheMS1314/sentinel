"""Server-owned, owner-scoped merchant identity and dispatch binding."""

from alembic import op
import sqlalchemy as sa

revision = "0005_merchant_identity"
down_revision = "0004_shared_authority"
branch_labels = depends_on = None


def upgrade():
    bind = op.get_bind()
    specs = [
        ("transactions", "merchant_id", sa.String(), "", ""),
        ("transactions", "merchant_version", sa.Integer(), "0", 0),
        ("payments", "authorized_merchant_version", sa.Integer(), "0", 0),
    ]
    for table, name, typ, default, _ in specs:
        if name not in {c["name"] for c in sa.inspect(bind).get_columns(table)}:
            op.add_column(
                table, sa.Column(name, typ, nullable=False, server_default=default)
            )
    if "ix_merchant_activity" not in {
        i["name"] for i in sa.inspect(bind).get_indexes("transactions")
    }:
        op.create_index(
            "ix_merchant_activity",
            "transactions",
            ["merchant_id", "created_at", "agent_id"],
        )
    if "merchant_identities" not in sa.inspect(bind).get_table_names():
        op.create_table(
            "merchant_identities",
            sa.Column("merchant_id", sa.String(), primary_key=True),
            sa.Column("owner_id", sa.String(), nullable=False),
            sa.Column("legal_name", sa.String(), nullable=False),
            sa.Column("category", sa.String(), nullable=False),
            sa.Column("domain", sa.String(), nullable=False),
            sa.Column("operator_reference", sa.Text(), nullable=False),
            sa.Column("active", sa.Boolean(), nullable=False),
            sa.Column("version", sa.Integer(), nullable=False),
            sa.Column("approved_by", sa.String(), nullable=False),
            sa.Column("updated_at", sa.DateTime(), nullable=False),
        )
        op.create_index(
            "ix_merchant_identities_owner_id", "merchant_identities", ["owner_id"]
        )


def downgrade():
    raise RuntimeError(
        "Destructive downgrade disabled; restore a reviewed backup instead"
    )

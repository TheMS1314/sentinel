"""Add local intelligence evidence and independent dispatch controls."""

from alembic import op

revision = "0002_risk_intelligence"
down_revision = "0001_hardened"
branch_labels = depends_on = None


def upgrade():
    from migrations.frozen_schema import v2_metadata

    metadata = v2_metadata()

    bind = op.get_bind()
    for name in (
        "merchant_risk_profiles",
        "threat_indicators",
        "intelligence_assessments",
        "dispatch_guard",
        "dispatch_claims",
        "risk_control_events",
    ):
        metadata.tables[name].create(bind, checkfirst=True)


def downgrade():
    raise RuntimeError(
        "Destructive downgrade disabled; restore a reviewed backup instead"
    )

"""Immutable schema snapshots for released migration revisions. Do not import live ORM metadata."""

import sqlalchemy as sa


def v1_metadata():
    metadata = sa.MetaData()
    table = sa.Table(
        "policies",
        metadata,
        sa.Column("id", sa.Integer(), primary_key=True, nullable=False),
        sa.Column("policy_id", sa.String(), nullable=False),
        sa.Column("agent_id", sa.String(), nullable=False),
        sa.Column("owner_human_id", sa.String(), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("active", sa.Boolean(), nullable=False),
        sa.Column("policy_json", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=True),
        sa.UniqueConstraint("agent_id", "version", name="uq_policy_agent_version"),
    )
    sa.Index("ix_policies_agent_id", table.c.agent_id, unique=False)
    sa.Index("ix_policies_policy_id", table.c.policy_id, unique=True)
    sa.Index(
        "uq_policy_active",
        table.c.agent_id,
        unique=True,
        postgresql_where=sa.text("active"),
        sqlite_where=sa.text("active"),
    )
    table = sa.Table(
        "transactions",
        metadata,
        sa.Column("id", sa.Integer(), primary_key=True, nullable=False),
        sa.Column("request_id", sa.String(), nullable=False),
        sa.Column("agent_id", sa.String(), nullable=False),
        sa.Column("merchant_name", sa.String(), nullable=False),
        sa.Column("merchant_category", sa.String(), nullable=False),
        sa.Column("amount", sa.Numeric(precision=14, scale=2), nullable=False),
        sa.Column("requested_at_hour", sa.Integer(), nullable=False),
        sa.Column("requested_at_iso", sa.String(), nullable=False),
        sa.Column("stated_intent", sa.Text(), nullable=False),
        sa.Column("raw_agent_goal", sa.Text(), nullable=True),
        sa.Column("scenario_tag", sa.String(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=True),
        sa.CheckConstraint("amount > 0", name="ck_positive_amount"),
    )
    sa.Index("ix_agent_event", table.c.agent_id, table.c.requested_at_iso, unique=False)
    sa.Index("ix_transactions_agent_id", table.c.agent_id, unique=False)
    sa.Index("ix_transactions_request_id", table.c.request_id, unique=True)
    sa.Index("ix_transactions_requested_at_iso", table.c.requested_at_iso, unique=False)
    table = sa.Table(
        "decisions",
        metadata,
        sa.Column("id", sa.Integer(), primary_key=True, nullable=False),
        sa.Column(
            "request_id",
            sa.String(),
            sa.ForeignKey("transactions.request_id"),
            nullable=False,
        ),
        sa.Column("verdict", sa.String(), nullable=False),
        sa.Column("reasons", sa.JSON(), nullable=False),
        sa.Column("checked_rules", sa.JSON(), nullable=False),
        sa.Column("decided_by", sa.String(), nullable=False),
        sa.Column("similar_past_cases", sa.JSON(), nullable=True),
        sa.Column("confidence", sa.Float(), nullable=True),
        sa.Column("model_version", sa.String(), nullable=True),
        sa.Column("risk_score", sa.Float(), nullable=True),
        sa.Column("policy_id", sa.String(), nullable=True),
        sa.Column("policy_version", sa.Integer(), nullable=True),
        sa.Column("policy_snapshot", sa.JSON(), nullable=True),
        sa.Column("prompt_version", sa.String(), nullable=True),
        sa.Column("prompt_hash", sa.String(), nullable=True),
        sa.Column("llm_mode", sa.String(), nullable=True),
        sa.Column("token_usage", sa.JSON(), nullable=True),
        sa.Column("razorpay_status", sa.String(), nullable=True),
        sa.Column("razorpay_ref", sa.String(), nullable=True),
        sa.Column("human_override", sa.String(), nullable=True),
        sa.Column("resolved_by", sa.String(), nullable=True),
        sa.Column("resolved_at", sa.DateTime(), nullable=True),
        sa.Column("resolution_reason", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=True),
    )
    sa.Index("ix_decisions_request_id", table.c.request_id, unique=True)
    table = sa.Table(
        "payments",
        metadata,
        sa.Column(
            "request_id",
            sa.String(),
            sa.ForeignKey("transactions.request_id"),
            primary_key=True,
            nullable=False,
        ),
        sa.Column("state", sa.String(), nullable=False),
        sa.Column("amount_paise", sa.BigInteger(), nullable=False),
        sa.Column("budget_day", sa.String(), nullable=True),
        sa.Column("order_id", sa.String(), nullable=True),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("updated_at", sa.DateTime(), nullable=True),
        sa.UniqueConstraint("order_id", name=None),
    )
    sa.Index("ix_payments_state", table.c.state, unique=False)
    table = sa.Table(
        "review_events",
        metadata,
        sa.Column("id", sa.Integer(), primary_key=True, nullable=False),
        sa.Column(
            "request_id",
            sa.String(),
            sa.ForeignKey("transactions.request_id"),
            nullable=False,
        ),
        sa.Column("reviewer_id", sa.String(), nullable=False),
        sa.Column("verdict", sa.String(), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=True),
        sa.UniqueConstraint("request_id", name=None),
    )
    table = sa.Table(
        "browser_sessions",
        metadata,
        sa.Column("token_hash", sa.String(), primary_key=True, nullable=False),
        sa.Column("credential_hash", sa.String(), nullable=False),
        sa.Column("expires_at", sa.DateTime(), nullable=False),
    )
    table = sa.Table(
        "provider_events",
        metadata,
        sa.Column("event_id", sa.String(), primary_key=True, nullable=False),
        sa.Column("payload_hash", sa.String(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=True),
    )
    return metadata


def v2_metadata():
    metadata = sa.MetaData()
    sa.Table(
        "transactions", metadata, sa.Column("request_id", sa.String(), primary_key=True)
    )
    table = sa.Table(
        "merchant_risk_profiles",
        metadata,
        sa.Column("id", sa.Integer(), primary_key=True, nullable=False),
        sa.Column("owner_id", sa.String(), nullable=False),
        sa.Column("merchant_key", sa.String(), nullable=False),
        sa.Column("merchant_name", sa.String(), nullable=False),
        sa.Column("domain", sa.String(), nullable=False),
        sa.Column("cyber_risk_score", sa.Integer(), nullable=False),
        sa.Column("source", sa.String(), nullable=False),
        sa.Column("evidence", sa.Text(), nullable=False),
        sa.Column("expires_at", sa.DateTime(), nullable=False),
        sa.Column("updated_by", sa.String(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.UniqueConstraint("owner_id", "merchant_key", name="uq_owner_merchant"),
    )
    sa.Index("ix_merchant_risk_profiles_owner_id", table.c.owner_id, unique=False)
    table = sa.Table(
        "threat_indicators",
        metadata,
        sa.Column("indicator_id", sa.String(), primary_key=True, nullable=False),
        sa.Column("domain", sa.String(), nullable=False),
        sa.Column("source", sa.String(), nullable=False),
        sa.Column("evidence", sa.Text(), nullable=False),
        sa.Column("confirmed", sa.Boolean(), nullable=False),
        sa.Column("active", sa.Boolean(), nullable=False),
        sa.Column("expires_at", sa.DateTime(), nullable=False),
        sa.Column("updated_by", sa.String(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
    )
    sa.Index("ix_threat_indicators_domain", table.c.domain, unique=False)
    table = sa.Table(
        "intelligence_assessments",
        metadata,
        sa.Column(
            "request_id",
            sa.String(),
            sa.ForeignKey("transactions.request_id", ondelete="CASCADE"),
            primary_key=True,
            nullable=False,
        ),
        sa.Column("report", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
    )
    table = sa.Table(
        "dispatch_guard",
        metadata,
        sa.Column("id", sa.Integer(), primary_key=True, nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("frozen", sa.Boolean(), nullable=False),
        sa.Column("window_seconds", sa.Integer(), nullable=False),
        sa.Column("max_dispatches", sa.Integer(), nullable=True),
        sa.Column("max_amount_paise", sa.BigInteger(), nullable=True),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("updated_by", sa.String(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
    )
    table = sa.Table(
        "dispatch_claims",
        metadata,
        sa.Column(
            "request_id",
            sa.String(),
            sa.ForeignKey("transactions.request_id", ondelete="CASCADE"),
            primary_key=True,
            nullable=False,
        ),
        sa.Column("amount_paise", sa.BigInteger(), nullable=False),
        sa.Column("claimed_at", sa.DateTime(), nullable=False),
    )
    sa.Index("ix_dispatch_claims_claimed_at", table.c.claimed_at, unique=False)
    table = sa.Table(
        "risk_control_events",
        metadata,
        sa.Column("id", sa.Integer(), primary_key=True, nullable=False),
        sa.Column("actor_id", sa.String(), nullable=False),
        sa.Column("kind", sa.String(), nullable=False),
        sa.Column("entity_id", sa.String(), nullable=False),
        sa.Column("before", sa.JSON(), nullable=True),
        sa.Column("after", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
    )
    return metadata

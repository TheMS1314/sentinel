"""Persistent policies, requests, decisions, reservations and review/session events.

Database records support audit reconstruction; database administrators are still
trusted. Apply Alembic migrations before starting an existing installation.
"""

import os
from datetime import datetime, timezone
from sqlalchemy import (
    create_engine,
    Column,
    String,
    Integer,
    BigInteger,
    Boolean,
    DateTime,
    ForeignKey,
    JSON,
    Text,
    Numeric,
    Float,
    UniqueConstraint,
    Index,
    CheckConstraint,
    text,
)
from sqlalchemy import event
from sqlalchemy.orm import declarative_base, relationship, sessionmaker


def now_utc():
    return datetime.now(timezone.utc)


DATABASE_URL = os.environ.get(
    "SENTINEL_DATABASE_URL", "postgresql://sentinel:sentinel@localhost:5432/sentinel"
)
from app.core.settings import database_options, dispatch_deadline

engine = create_engine(DATABASE_URL, **database_options(DATABASE_URL))

if DATABASE_URL.startswith("sqlite"):

    @event.listens_for(engine, "connect")
    def sqlite_integrity(connection, record):
        connection.execute("PRAGMA foreign_keys=ON")


SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
Base = declarative_base()


class PolicyRecord(Base):
    __tablename__ = "policies"
    __table_args__ = (
        UniqueConstraint("agent_id", "version", name="uq_policy_agent_version"),
        Index(
            "uq_policy_active",
            "agent_id",
            unique=True,
            postgresql_where=text("active"),
            sqlite_where=text("active"),
        ),
    )
    id = Column(Integer, primary_key=True)
    policy_id = Column(String, unique=True, index=True, nullable=False)
    agent_id = Column(String, index=True, nullable=False)
    owner_human_id = Column(String, nullable=False)
    version = Column(Integer, default=1, nullable=False)
    active = Column(Boolean, default=True, nullable=False)
    policy_json = Column(JSON, nullable=False)
    created_at = Column(DateTime, default=now_utc)


class TransactionRecord(Base):
    __tablename__ = "transactions"
    __table_args__ = (
        CheckConstraint("amount > 0", name="ck_positive_amount"),
        Index("ix_agent_event", "agent_id", "requested_at_iso"),
        Index("ix_transaction_processing", "agent_id", "created_at"),
        Index("ix_merchant_activity", "merchant_id", "created_at", "agent_id"),
    )
    id = Column(Integer, primary_key=True)
    request_id = Column(String, unique=True, index=True, nullable=False)
    agent_id = Column(String, index=True, nullable=False)
    merchant_name = Column(String, nullable=False)
    merchant_category = Column(String, nullable=False)
    merchant_id = Column(String, nullable=False, default="")
    merchant_version = Column(Integer, nullable=False, default=0)
    amount = Column(Numeric(14, 2), nullable=False)
    requested_at_hour = Column(Integer, nullable=False)
    requested_at_iso = Column(String, index=True, nullable=False)
    stated_intent = Column(Text, nullable=False)
    raw_agent_goal = Column(Text, default="")
    scenario_tag = Column(String, default="")
    submitted_by = Column(String, default="internal", nullable=False)
    submitted_credential_version = Column(Integer, default=1, nullable=False)
    created_at = Column(DateTime, default=now_utc)
    decision = relationship(
        "DecisionRecord", back_populates="transaction", uselist=False
    )
    intelligence = relationship(
        "IntelligenceAssessment", uselist=False, passive_deletes=True
    )


class DecisionRecord(Base):
    __tablename__ = "decisions"
    id = Column(Integer, primary_key=True)
    request_id = Column(
        String,
        ForeignKey("transactions.request_id"),
        unique=True,
        index=True,
        nullable=False,
    )
    verdict = Column(String, nullable=False)
    reasons = Column(JSON, nullable=False)
    checked_rules = Column(JSON, nullable=False)
    decided_by = Column(String, nullable=False)
    similar_past_cases = Column(JSON, default=list)
    confidence = Column(Float, nullable=True)
    model_version = Column(String, nullable=True)
    risk_score = Column(Float, nullable=True)
    policy_id = Column(String, nullable=True)
    policy_version = Column(Integer, nullable=True)
    policy_snapshot = Column(JSON, nullable=True)
    prompt_version = Column(String, nullable=True)
    prompt_hash = Column(String, nullable=True)
    llm_mode = Column(String, nullable=True)
    token_usage = Column(JSON, nullable=True)
    razorpay_status = Column(String, default="not_sent")
    razorpay_ref = Column(String, nullable=True)
    human_override = Column(String, nullable=True)
    resolved_by = Column(String, nullable=True)
    resolved_at = Column(DateTime, nullable=True)
    resolution_reason = Column(Text, nullable=True)
    created_at = Column(DateTime, default=now_utc)
    transaction = relationship("TransactionRecord", back_populates="decision")


class PaymentRecord(Base):
    __tablename__ = "payments"
    __table_args__ = (Index("ix_payment_recovery", "state", "updated_at"),)
    request_id = Column(String, ForeignKey("transactions.request_id"), primary_key=True)
    state = Column(String, default="pending", nullable=False, index=True)
    amount_paise = Column(BigInteger, nullable=False)
    budget_day = Column(String, nullable=True)
    order_id = Column(String, unique=True, nullable=True)
    error = Column(Text, nullable=True)
    updated_at = Column(DateTime, default=now_utc, onupdate=now_utc)
    dispatch_expires_at = Column(DateTime, default=dispatch_deadline, nullable=True)
    authorization_policy_id = Column(String, nullable=True)
    authorizing_credential_version = Column(Integer, default=1, nullable=False)
    authorized_merchant_version = Column(Integer, default=0, nullable=False)


class ReviewEvent(Base):
    __tablename__ = "review_events"
    id = Column(Integer, primary_key=True)
    request_id = Column(
        String, ForeignKey("transactions.request_id"), nullable=False, unique=True
    )
    reviewer_id = Column(String, nullable=False)
    verdict = Column(String, nullable=False)
    reason = Column(Text, nullable=False)
    created_at = Column(DateTime, default=now_utc)


class BrowserSession(Base):
    __tablename__ = "browser_sessions"
    __table_args__ = (Index("ix_session_expiry", "expires_at"),)
    token_hash = Column(String, primary_key=True)
    credential_hash = Column(String, nullable=False)
    expires_at = Column(DateTime, nullable=False)


class ProviderEvent(Base):
    __tablename__ = "provider_events"
    event_id = Column(String, primary_key=True)
    payload_hash = Column(String, nullable=False)
    created_at = Column(DateTime, default=now_utc)


class MerchantRiskProfile(Base):
    __tablename__ = "merchant_risk_profiles"
    __table_args__ = (
        UniqueConstraint("owner_id", "merchant_key", name="uq_owner_merchant"),
    )
    id = Column(Integer, primary_key=True)
    owner_id = Column(String, nullable=False, index=True)
    merchant_key = Column(String, nullable=False)
    merchant_name = Column(String, nullable=False)
    domain = Column(String, nullable=False)
    cyber_risk_score = Column(Integer, nullable=False)
    source = Column(String, nullable=False)
    evidence = Column(Text, nullable=False)
    expires_at = Column(DateTime, nullable=False)
    updated_by = Column(String, nullable=False)
    updated_at = Column(DateTime, default=now_utc, nullable=False)


class ThreatIndicator(Base):
    __tablename__ = "threat_indicators"
    indicator_id = Column(String, primary_key=True)
    domain = Column(String, nullable=False, index=True)
    source = Column(String, nullable=False)
    evidence = Column(Text, nullable=False)
    confirmed = Column(Boolean, default=False, nullable=False)
    active = Column(Boolean, default=True, nullable=False)
    expires_at = Column(DateTime, nullable=False)
    updated_by = Column(String, nullable=False)
    updated_at = Column(DateTime, default=now_utc, nullable=False)


class IntelligenceAssessment(Base):
    __tablename__ = "intelligence_assessments"
    request_id = Column(
        String,
        ForeignKey("transactions.request_id", ondelete="CASCADE"),
        primary_key=True,
    )
    report = Column(JSON, nullable=False)
    created_at = Column(DateTime, default=now_utc, nullable=False)


class DispatchGuard(Base):
    __tablename__ = "dispatch_guard"
    id = Column(Integer, primary_key=True)
    version = Column(Integer, default=1, nullable=False)
    frozen = Column(Boolean, default=False, nullable=False)
    window_seconds = Column(Integer, default=60, nullable=False)
    max_dispatches = Column(Integer, nullable=True)
    max_amount_paise = Column(BigInteger, nullable=True)
    reason = Column(Text, nullable=False)
    updated_by = Column(String, nullable=False)
    updated_at = Column(DateTime, default=now_utc, nullable=False)


class DispatchClaim(Base):
    __tablename__ = "dispatch_claims"
    request_id = Column(
        String,
        ForeignKey("transactions.request_id", ondelete="CASCADE"),
        primary_key=True,
    )
    amount_paise = Column(BigInteger, nullable=False)
    claimed_at = Column(DateTime, default=now_utc, nullable=False, index=True)


class RiskControlEvent(Base):
    __tablename__ = "risk_control_events"
    id = Column(Integer, primary_key=True)
    actor_id = Column(String, nullable=False)
    kind = Column(String, nullable=False)
    entity_id = Column(String, nullable=False)
    before = Column(JSON, nullable=True)
    after = Column(JSON, nullable=False)
    created_at = Column(DateTime, default=now_utc, nullable=False)


def init_db():
    Base.metadata.create_all(engine)


def get_session():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


class EvaluationJob(Base):
    __tablename__ = "evaluation_jobs"
    __table_args__ = (
        Index("ix_job_claim", "status", "lease_expires_at", "created_at"),
        Index("ix_job_batch", "batch_id", "created_at"),
        Index("ix_job_retention", "completed_at"),
    )
    job_id = Column(String, primary_key=True)
    batch_id = Column(String, nullable=True)
    submitted_by = Column(String, nullable=False)
    agent_id = Column(String, nullable=False)
    payload = Column(JSON, nullable=False)
    status = Column(String, default="QUEUED", nullable=False)
    result = Column(JSON, nullable=True)
    error = Column(String, nullable=True)
    attempts = Column(Integer, default=0, nullable=False)
    lease_token = Column(String, nullable=True)
    lease_expires_at = Column(DateTime, nullable=True)
    created_at = Column(DateTime, default=now_utc, nullable=False)
    updated_at = Column(DateTime, default=now_utc, onupdate=now_utc, nullable=False)
    completed_at = Column(DateTime, nullable=True)


class ServiceCredential(Base):
    __tablename__ = "service_credentials"
    subject = Column(String, primary_key=True)
    key_hash = Column(String, nullable=False, unique=True, index=True)
    role = Column(String, nullable=False)
    owner_id = Column(String, nullable=False, default="")
    agent_ids = Column(JSON, nullable=False, default=list)
    active = Column(Boolean, nullable=False, default=True)
    expires_at = Column(DateTime, nullable=True)
    version = Column(Integer, nullable=False, default=1)
    updated_by = Column(String, nullable=False)
    updated_at = Column(DateTime, nullable=False, default=now_utc)


class MerchantIdentity(Base):
    __tablename__ = "merchant_identities"
    merchant_id = Column(String, primary_key=True)
    owner_id = Column(String, nullable=False, index=True)
    legal_name = Column(String, nullable=False)
    category = Column(String, nullable=False)
    domain = Column(String, nullable=False)
    operator_reference = Column(Text, nullable=False)
    active = Column(Boolean, nullable=False, default=True)
    version = Column(Integer, nullable=False, default=1)
    approved_by = Column(String, nullable=False)
    updated_at = Column(DateTime, nullable=False, default=now_utc)

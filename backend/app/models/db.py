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
engine = create_engine(
    DATABASE_URL,
    pool_pre_ping=True,
    connect_args={"check_same_thread": False, "timeout": 30}
    if DATABASE_URL.startswith("sqlite")
    else {},
)
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
    )
    id = Column(Integer, primary_key=True)
    request_id = Column(String, unique=True, index=True, nullable=False)
    agent_id = Column(String, index=True, nullable=False)
    merchant_name = Column(String, nullable=False)
    merchant_category = Column(String, nullable=False)
    amount = Column(Numeric(14, 2), nullable=False)
    requested_at_hour = Column(Integer, nullable=False)
    requested_at_iso = Column(String, index=True, nullable=False)
    stated_intent = Column(Text, nullable=False)
    raw_agent_goal = Column(Text, default="")
    scenario_tag = Column(String, default="")
    created_at = Column(DateTime, default=now_utc)
    decision = relationship(
        "DecisionRecord", back_populates="transaction", uselist=False
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
    request_id = Column(String, ForeignKey("transactions.request_id"), primary_key=True)
    state = Column(String, default="pending", nullable=False, index=True)
    amount_paise = Column(BigInteger, nullable=False)
    budget_day = Column(String, nullable=True)
    order_id = Column(String, unique=True, nullable=True)
    error = Column(Text, nullable=True)
    updated_at = Column(DateTime, default=now_utc, onupdate=now_utc)


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
    token_hash = Column(String, primary_key=True)
    credential_hash = Column(String, nullable=False)
    expires_at = Column(DateTime, nullable=False)


class ProviderEvent(Base):
    __tablename__ = "provider_events"
    event_id = Column(String, primary_key=True)
    payload_hash = Column(String, nullable=False)
    created_at = Column(DateTime, default=now_utc)


def init_db():
    Base.metadata.create_all(engine)


def get_session():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()

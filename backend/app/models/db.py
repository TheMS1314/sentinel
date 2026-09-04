"""
Sentinel Database Layer
========================
SQLAlchemy models backed by Postgres. Three tables:

  - policies      : versioned agent spending policies
  - transactions   : every transaction request Sentinel has ever seen
  - decisions      : the verdict + reasoning for each transaction

Design note: transactions and decisions are separate tables (1:1) rather
than one merged table, because it keeps the audit trail append-only and
clean -- a transaction request is a fact, a decision is a judgment made
about that fact, and keeping them distinct makes the eval harness (Day 9-10)
simpler: we can compare "decision.verdict" against a golden label without
touching the request record at all.
"""

from __future__ import annotations
import os
from datetime import datetime

from sqlalchemy import (
    create_engine, Column, String, Float, Integer, Boolean, DateTime,
    ForeignKey, JSON, Text
)
from sqlalchemy.orm import declarative_base, relationship, sessionmaker

DATABASE_URL = os.environ.get(
    "SENTINEL_DATABASE_URL",
    "postgresql://sentinel:sentinel@localhost:5432/sentinel",
)

engine = create_engine(DATABASE_URL, pool_pre_ping=True)
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
Base = declarative_base()


class PolicyRecord(Base):
    __tablename__ = "policies"

    id = Column(Integer, primary_key=True, autoincrement=True)
    policy_id = Column(String, unique=True, index=True, nullable=False)
    agent_id = Column(String, index=True, nullable=False)
    owner_human_id = Column(String, nullable=False)
    version = Column(Integer, default=1)
    active = Column(Boolean, default=True)
    policy_json = Column(JSON, nullable=False)  # full AgentPolicy.to_dict()
    created_at = Column(DateTime, default=datetime.utcnow)


class TransactionRecord(Base):
    __tablename__ = "transactions"

    id = Column(Integer, primary_key=True, autoincrement=True)
    request_id = Column(String, unique=True, index=True, nullable=False)
    agent_id = Column(String, index=True, nullable=False)
    merchant_name = Column(String, nullable=False)
    merchant_category = Column(String, nullable=False)
    amount = Column(Float, nullable=False)
    requested_at_hour = Column(Integer, nullable=False)
    requested_at_iso = Column(String, nullable=False, index=True)
    stated_intent = Column(Text, nullable=False)
    raw_agent_goal = Column(Text, default="")
    scenario_tag = Column(String, default="")  # e.g. "well_behaved" / "adversarial:velocity_spike"
    created_at = Column(DateTime, default=datetime.utcnow)

    decision = relationship("DecisionRecord", back_populates="transaction", uselist=False)


class DecisionRecord(Base):
    __tablename__ = "decisions"

    id = Column(Integer, primary_key=True, autoincrement=True)
    request_id = Column(String, ForeignKey("transactions.request_id"), unique=True, index=True, nullable=False)
    verdict = Column(String, nullable=False)          # APPROVE / HOLD / BLOCK
    reasons = Column(JSON, nullable=False)             # list[str]
    checked_rules = Column(JSON, nullable=False)       # list[str]
    decided_by = Column(String, nullable=False)        # rule_engine / llm_reasoner
    similar_past_cases = Column(JSON, default=list)    # RAG retrieval refs
    confidence = Column(Float, nullable=True)
    model_version = Column(String, nullable=True)  # e.g. "anthropic:claude-sonnet-4-6" or "mock"
    razorpay_status = Column(String, default="not_sent")  # not_sent / sent / failed
    razorpay_ref = Column(String, nullable=True)
    human_override = Column(String, nullable=True)     # if a HOLD was resolved by a human: APPROVE/BLOCK
    created_at = Column(DateTime, default=datetime.utcnow)

    transaction = relationship("TransactionRecord", back_populates="decision")


def init_db():
    """Create all tables. Safe to call repeatedly (no-op if they exist)."""
    Base.metadata.create_all(bind=engine)


def get_session():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()

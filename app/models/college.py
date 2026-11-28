"""Colleges and their distribution controls."""

from __future__ import annotations

from sqlalchemy import (
    Boolean,
    Column,
    DateTime,
    Integer,
    String,
)

from app.models.base import Base, gen_uuid, utcnow


class College(Base):
    __tablename__ = "colleges"

    id = Column(String(36), primary_key=True, default=gen_uuid)
    name = Column(String(200), nullable=False)
    #: Optional. Present only when the college issues student mailboxes and an admin has
    #: verified ownership — it is no longer proof of enrolment on its own (PLAN.md §7.3).
    domain = Column(String(100), nullable=True, unique=True)
    domain_verified_at = Column(DateTime, nullable=True)
    plan = Column(Integer, default=100)
    total_credits = Column(Integer, default=500)  # legacy; superseded by token wallets
    credits_used = Column(Integer, default=0)  # legacy
    handler_email = Column(String(200), nullable=True)
    handler_id = Column(String(36), nullable=True)
    credit_limit = Column(Integer, default=5)  # legacy
    is_active = Column(Boolean, default=True)
    created_at = Column(DateTime, default=utcnow)

    # ── Added in Phase 1 ──────────────────────────────────────────────────────
    monthly_allowance_default = Column(Integer, default=4, nullable=False)
    daily_interview_cap = Column(Integer, default=3, nullable=False)
    #: Admin can freeze a college's ability to distribute allowances.
    distribution_frozen = Column(Boolean, default=False, nullable=False)
    #: Does the student's consent allow this college to view proctoring snapshots?
    snapshot_sharing_allowed = Column(Boolean, default=False, nullable=False)
    deleted_at = Column(DateTime, nullable=True)
    updated_at = Column(DateTime, default=utcnow, onupdate=utcnow)

"""Verification, enrolment, consent and audit (PLAN.md §7.2, §7.3).

Enrolment is the point of this module: typing an email address is never proof that a
student belongs to a college. A roster row plus a verified OTP is, and that is what these
tables record.

Secrets are never stored in the clear — `verification_codes.code_hash` holds a keyed hash,
so a database leak does not hand over live OTPs or OAuth hand-off codes.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import (
    JSON,
    Boolean,
    CheckConstraint,
    Column,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)

from app.models.base import Base, gen_uuid, utcnow


class VerificationCode(Base):
    """A single-use OTP, magic link, or OAuth hand-off code."""

    __tablename__ = "verification_codes"

    id = Column(String(36), primary_key=True, default=gen_uuid)
    #: 'email_otp' | 'phone_otp' | 'magic_link' | 'oauth_handoff' | 'admin_invite'
    purpose = Column(String(20), nullable=False)
    #: Email address, phone number, or user id — whatever the code is bound to.
    subject = Column(String(200), nullable=False, index=True)
    code_hash = Column(String(64), nullable=False, index=True)
    student_id = Column(String(36), nullable=True, index=True)
    payload = Column(JSON, nullable=True)
    attempts = Column(Integer, default=0, nullable=False)
    max_attempts = Column(Integer, default=5, nullable=False)
    expires_at = Column(DateTime, nullable=False)
    consumed_at = Column(DateTime, nullable=True)
    request_ip = Column(String(64), nullable=True)
    created_at = Column(DateTime, default=utcnow, index=True)

    __table_args__ = (
        CheckConstraint(
            "purpose in ('email_otp','phone_otp','magic_link','oauth_handoff',"
            "'admin_invite')",
            name="ck_verification_codes_purpose",
        ),
        Index("ix_verification_codes_lookup", "purpose", "subject", "consumed_at"),
    )

    def is_usable(self, now: datetime) -> bool:
        return (
            self.consumed_at is None
            and self.attempts < self.max_attempts
            and self.expires_at > now
        )


class RosterEntry(Base):
    """A student the college says belongs to it. Matched against a *verified* contact."""

    __tablename__ = "roster_entries"

    id = Column(String(36), primary_key=True, default=gen_uuid)
    college_id = Column(
        String(36), ForeignKey("colleges.id"), nullable=False, index=True
    )
    enrollment_no = Column(String(60), nullable=True)
    name = Column(String(200), nullable=True)
    batch = Column(String(40), nullable=True)
    branch = Column(String(100), nullable=True)
    email = Column(String(200), nullable=True, index=True)
    phone = Column(String(20), nullable=True, index=True)
    #: Batch end. An expired row loses pool access automatically.
    valid_until = Column(DateTime, nullable=True)
    is_active = Column(Boolean, default=True, nullable=False)
    #: Set once a real account claims this row.
    claimed_by = Column(String(36), nullable=True, index=True)
    claimed_at = Column(DateTime, nullable=True)
    uploaded_by = Column(String(36), nullable=True)
    created_at = Column(DateTime, default=utcnow)

    __table_args__ = (
        UniqueConstraint(
            "college_id", "enrollment_no", name="uq_roster_college_enrollment"
        ),
        Index("ix_roster_college_email", "college_id", "email"),
        Index("ix_roster_college_phone", "college_id", "phone"),
    )


class JoinCode(Base):
    """Never sufficient on its own — always paired with a roster match or approval."""

    __tablename__ = "join_codes"

    id = Column(String(36), primary_key=True, default=gen_uuid)
    college_id = Column(
        String(36), ForeignKey("colleges.id"), nullable=False, index=True
    )
    code = Column(String(32), nullable=False, unique=True)
    #: 'roster_match' | 'admin_approval' — what else the student must satisfy.
    requires = Column(String(20), nullable=False, default="roster_match")
    max_uses = Column(Integer, nullable=True)
    uses = Column(Integer, default=0, nullable=False)
    expires_at = Column(DateTime, nullable=True)
    is_active = Column(Boolean, default=True, nullable=False)
    created_by = Column(String(36), nullable=True)
    created_at = Column(DateTime, default=utcnow)

    __table_args__ = (
        CheckConstraint(
            "requires in ('roster_match','admin_approval')",
            name="ck_join_codes_requires",
        ),
    )


class EnrollmentRequest(Base):
    """Queue for students who need a college admin's approval to join."""

    __tablename__ = "enrollment_requests"

    id = Column(String(36), primary_key=True, default=gen_uuid)
    college_id = Column(
        String(36), ForeignKey("colleges.id"), nullable=False, index=True
    )
    student_id = Column(String(36), nullable=False, index=True)
    #: 'roster' | 'domain' | 'join_code'
    method = Column(String(20), nullable=False)
    evidence = Column(JSON, nullable=True)
    status = Column(String(16), nullable=False, default="pending")
    decided_by = Column(String(36), nullable=True)
    decided_at = Column(DateTime, nullable=True)
    note = Column(Text, nullable=True)
    created_at = Column(DateTime, default=utcnow)

    __table_args__ = (
        CheckConstraint(
            "status in ('pending','approved','rejected')",
            name="ck_enrollment_requests_status",
        ),
        UniqueConstraint(
            "college_id", "student_id", name="uq_enrollment_requests_pair"
        ),
    )


class Consent(Base):
    """Recorded consent, versioned so we know which notice text was agreed to."""

    __tablename__ = "consents"

    id = Column(String(36), primary_key=True, default=gen_uuid)
    student_id = Column(String(36), nullable=False, index=True)
    #: 'proctoring' | 'profile_processing' | 'college_sharing'
    kind = Column(String(30), nullable=False)
    version = Column(String(20), nullable=False)
    granted_at = Column(DateTime, default=utcnow, nullable=False)
    revoked_at = Column(DateTime, nullable=True)
    ip = Column(String(64), nullable=True)
    user_agent = Column(String(400), nullable=True)

    __table_args__ = (
        CheckConstraint(
            "kind in ('proctoring','profile_processing','college_sharing')",
            name="ck_consents_kind",
        ),
        Index("ix_consents_student_kind", "student_id", "kind", "revoked_at"),
    )


class ApiAuditLog(Base):
    """Every admin and college-admin action, token movement, and report view.

    Deliberately append-only and never exposed to a mutating endpoint: this is the record
    that explains, after the fact, who moved a college's tokens and who read a student's
    report.
    """

    __tablename__ = "api_audit_log"

    id = Column(String(36), primary_key=True, default=gen_uuid)
    actor_id = Column(String(36), nullable=True, index=True)
    actor_role = Column(String(20), nullable=True)
    action = Column(String(80), nullable=False, index=True)
    #: Free-form resource identifier, e.g. "college:abc123" or "session:def456".
    target = Column(String(120), nullable=True, index=True)
    detail = Column(JSON, nullable=True)
    ip = Column(String(64), nullable=True)
    request_id = Column(String(64), nullable=True)
    created_at = Column(DateTime, default=utcnow, index=True)

    __table_args__ = (
        Index("ix_api_audit_actor_created", "actor_id", "created_at"),
    )

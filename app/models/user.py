"""Platform users.

`students` is the legacy table name; it holds students, college admins and
admins. `role` is only ever read through `app.core.roles.normalize_role`, because
rows may still carry the pre-rename `officer` / `super_admin` values.
"""

from __future__ import annotations

from sqlalchemy import (
    JSON,
    Boolean,
    Column,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    String,
    Text,
)

from app.models.base import Base, gen_uuid, utcnow


class Student(Base):
    """A platform user. Legacy table name; holds students, college admins and admins.

    `role` is stored here but is only ever read through `core.roles.normalize_role`,
    because rows may still contain the legacy `officer` / `super_admin` values.
    """

    __tablename__ = "students"

    id = Column(String(36), primary_key=True, default=gen_uuid)
    name = Column(String(200))
    email = Column(String(200), unique=True)
    role = Column(String(20), default="student", nullable=False)
    cgpa = Column(Float, default=7.0)
    branch = Column(String(100), default="Computer Science")
    year_of_passing = Column(Integer, default=2026)
    skills = Column(JSON, default=list)
    resume_text = Column(Text, nullable=True)
    password_hash = Column(String(200), nullable=True)
    college_id = Column(String(36), ForeignKey("colleges.id"), nullable=True, index=True)
    created_at = Column(DateTime, default=utcnow)

    # ── Added in Phase 1 ──────────────────────────────────────────────────────
    phone = Column(String(20), nullable=True, unique=True)
    email_verified_at = Column(DateTime, nullable=True)
    phone_verified_at = Column(DateTime, nullable=True)
    #: 'bcrypt' | 'sha256' (legacy, upgraded on next successful login) | 'google_oauth'
    password_algo = Column(String(20), default="bcrypt", nullable=False)
    #: Bumped to invalidate every outstanding JWT for this user.
    token_version = Column(Integer, default=0, nullable=False)
    is_active = Column(Boolean, default=True, nullable=False)
    deleted_at = Column(DateTime, nullable=True)
    mfa_secret = Column(String(64), nullable=True)
    mfa_enabled = Column(Boolean, default=False, nullable=False)
    #: 'roster' | 'domain' | 'join_code' | 'direct'
    enrolled_via = Column(String(20), nullable=True)
    updated_at = Column(DateTime, default=utcnow, onupdate=utcnow)

    @property
    def is_email_verified(self) -> bool:
        return self.email_verified_at is not None

    @property
    def is_phone_verified(self) -> bool:
        return self.phone_verified_at is not None

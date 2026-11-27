"""Engine, session factory, and the core domain tables.

Changes from the previous version:

* The engine is configured from `core.config.settings` with a small pool, because the app
  now runs as many short-lived Vercel functions behind the Supabase pooler rather than one
  long-lived Render process (PLAN.md §0.1).
* There is no `create_tables()` any more. Schema changes go through Alembic; the app never
  issues DDL at import or request time.
* JD seed data moved to `data/jd_templates.py` and is applied by `scripts/seed.py`, not on
  startup.

Token, billing and identity tables live in sibling modules and are imported here so that a
single `import models` registers every table with `Base.metadata` for Alembic autogenerate.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

from sqlalchemy import (
    JSON,
    Boolean,
    Column,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    create_engine,
)
from sqlalchemy.orm import declarative_base, sessionmaker

from core.config import settings

# ── Engine ────────────────────────────────────────────────────────────────────

_url = settings.sqlalchemy_url or "sqlite:///./placeprep.db"
_is_sqlite = _url.startswith("sqlite")

if _is_sqlite:
    engine = create_engine(_url, connect_args={"check_same_thread": False})
else:
    engine = create_engine(
        _url,
        pool_pre_ping=True,
        pool_size=settings.DB_POOL_SIZE,
        max_overflow=settings.DB_MAX_OVERFLOW,
        pool_recycle=settings.DB_POOL_RECYCLE_SEC,
        connect_args={
            # Never let one bad query hold a pooler connection open indefinitely.
            "options": f"-c statement_timeout={settings.DB_STATEMENT_TIMEOUT_MS}",
            "application_name": f"placeprep-{settings.ENV}",
        },
    )

SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
Base = declarative_base()


def gen_uuid() -> str:
    return str(uuid.uuid4())


def utcnow() -> datetime:
    """Timezone-aware UTC now.

    The existing columns are `timestamp without time zone`; we keep writing naive UTC to
    stay compatible with the 67 sessions and 173 transcripts already stored.
    """
    return datetime.now(UTC).replace(tzinfo=None)


# ── People ────────────────────────────────────────────────────────────────────


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


# ── Job descriptions ──────────────────────────────────────────────────────────


class JobDescription(Base):
    __tablename__ = "job_descriptions"

    id = Column(String(36), primary_key=True, default=gen_uuid)
    company_name = Column(String(200))
    role_title = Column(String(200))
    required_skills = Column(JSON, default=list)
    cgpa_cutoff = Column(Float, default=6.0)
    jd_text = Column(Text)
    difficulty = Column(String(20), default="medium")
    duration_minutes = Column(Integer, default=22)
    is_active = Column(Boolean, default=True)
    created_at = Column(DateTime, default=utcnow)
    requirements_extracted_at = Column(DateTime, nullable=True)


class JDChunk(Base):
    __tablename__ = "jd_chunks"

    id = Column(String(36), primary_key=True, default=gen_uuid)
    jd_id = Column(String(36), index=True)
    chunk_text = Column(Text)
    chunk_index = Column(Integer)


class StudentJD(Base):
    """A JD pasted by a student, plus the LLM resume-match result."""

    __tablename__ = "student_jds"

    id = Column(String(36), primary_key=True, default=gen_uuid)
    student_id = Column(String(36), nullable=False, index=True)
    company_name = Column(String(200), nullable=False)
    role_title = Column(String(200), nullable=False)
    jd_text = Column(Text, nullable=False)
    match_pct = Column(Integer, default=0)
    matched_skills = Column(Text, default="[]")
    missing_skills = Column(Text, default="[]")
    advice = Column(Text, default="")
    status = Column(String(20), default="pending")
    created_at = Column(DateTime, default=utcnow)


# ── Interviews ────────────────────────────────────────────────────────────────


class InterviewSession(Base):
    __tablename__ = "interview_sessions"

    id = Column(String(36), primary_key=True, default=gen_uuid)
    student_id = Column(String(36))
    jd_id = Column(String(36))
    status = Column(String(20), default="active")
    started_at = Column(DateTime, default=utcnow)
    ended_at = Column(DateTime, nullable=True)
    duration_seconds = Column(Integer, nullable=True)
    question_count = Column(Integer, default=0)
    total_filler_words = Column(Integer, default=0)
    avg_wpm = Column(Float, nullable=True)

    # ── Added in Phase 1 ──────────────────────────────────────────────────────
    #: True when a custom student JD was used (jd_id then points at student_jds).
    is_custom_jd = Column(Boolean, default=False, nullable=False)
    #: Set when the webcam was unavailable — the session still runs, unproctored.
    proctored = Column(Boolean, default=True, nullable=False)
    integrity_score = Column(Float, nullable=True)
    #: 'completed' | 'student_ended' | 'auto_terminated' | 'server_error' | 'abandoned'
    ended_reason = Column(String(30), nullable=True)
    plan_version = Column(Integer, default=1, nullable=False)
    answered_count = Column(Integer, default=0, nullable=False)

    __table_args__ = (
        Index("ix_interview_sessions_student_started", "student_id", "started_at"),
        Index("ix_interview_sessions_jd", "jd_id"),
    )


class SessionTranscript(Base):
    __tablename__ = "session_transcripts"

    id = Column(String(36), primary_key=True, default=gen_uuid)
    session_id = Column(String(36))
    turn_number = Column(Integer)
    phase = Column(String(20))
    question = Column(Text)
    answer = Column(Text)
    answer_wpm = Column(Float, nullable=True)
    filler_count = Column(Integer, default=0)
    created_at = Column(DateTime, default=utcnow)
    #: Client-supplied idempotency key for the turn endpoint that replaced the WebSocket.
    turn_id = Column(String(64), nullable=True)
    #: 'browser_stt' | 'whisper' | 'text'
    transcript_source = Column(String(20), nullable=True)

    __table_args__ = (
        Index("ix_session_transcripts_session_turn", "session_id", "turn_number"),
        Index("uq_session_transcripts_turn_id", "session_id", "turn_id", unique=True),
    )


class SessionScore(Base):
    __tablename__ = "session_scores"

    id = Column(String(36), primary_key=True, default=gen_uuid)
    session_id = Column(String(36), unique=True)
    overall_score = Column(Float, nullable=True)
    technical_score = Column(Float, nullable=True)
    communication_score = Column(Float, nullable=True)
    body_language_score = Column(Float, nullable=True)
    avg_eye_contact_pct = Column(Float, nullable=True)
    avg_posture_score = Column(Float, nullable=True)
    verdict = Column(String(50), nullable=True)
    topics_json = Column(JSON, nullable=True)
    strengths_json = Column(JSON, nullable=True)
    improvements_json = Column(JSON, nullable=True)
    tips_json = Column(JSON, nullable=True)
    evaluation_status = Column(String(20), default="pending")
    evaluated_at = Column(DateTime, nullable=True)

    # ── Added in Phase 1 / used from Phase 3 ──────────────────────────────────
    competency_scores = Column(JSON, nullable=True)
    weighted_total = Column(Float, nullable=True)
    #: Kept deliberately separate from overall_score (PLAN.md §4 Phase 3).
    integrity_score = Column(Float, nullable=True)
    #: 1 = legacy single-call evaluation, 2 = Phase 3 engine. Lets the report renderer
    #: show historical sessions correctly instead of rewriting their meaning.
    scoring_version = Column(Integer, default=1, nullable=False)


class Shortlist(Base):
    __tablename__ = "shortlists"

    id = Column(String(36), primary_key=True, default=gen_uuid)
    jd_id = Column(String(36))
    officer_id = Column(String(36))
    status = Column(String(20), default="processing")
    results_json = Column(JSON, nullable=True)
    created_at = Column(DateTime, default=utcnow)


class StudentProfileData(Base):
    """Scraped GitHub/LeetCode/portfolio summary, keyed by student."""

    __tablename__ = "student_profile_data"

    student_id = Column(String(36), primary_key=True)
    scraped_summary = Column(Text)
    urls_found = Column(Integer, default=0)
    updated_at = Column(DateTime, default=utcnow, onupdate=utcnow)


# Importing the sibling modules registers their tables on Base.metadata, which Alembic
# autogenerate and the test fixtures both rely on. Keep at the bottom: they import Base
# from this module.
from models import billing, identity, proctoring, tokens  # noqa: E402,F401

__all__ = [
    "Base",
    "College",
    "InterviewSession",
    "JDChunk",
    "JobDescription",
    "SessionLocal",
    "SessionScore",
    "SessionTranscript",
    "Shortlist",
    "Student",
    "StudentJD",
    "StudentProfileData",
    "billing",
    "engine",
    "gen_uuid",
    "identity",
    "proctoring",
    "tokens",
    "utcnow",
]

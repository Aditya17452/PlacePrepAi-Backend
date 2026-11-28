"""Interview sessions, transcripts, scores and the scraped candidate profile."""

from __future__ import annotations

from sqlalchemy import (
    JSON,
    Boolean,
    Column,
    DateTime,
    Float,
    Index,
    Integer,
    String,
    Text,
)

from app.models.base import Base, gen_uuid, utcnow


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

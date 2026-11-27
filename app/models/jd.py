"""Job descriptions: the seeded roles, their retrieval chunks, and student-pasted JDs."""

from __future__ import annotations

from sqlalchemy import (
    JSON,
    Boolean,
    Column,
    DateTime,
    Float,
    Integer,
    String,
    Text,
)

from app.models.base import Base, gen_uuid, utcnow


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

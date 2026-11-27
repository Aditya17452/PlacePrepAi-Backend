"""Resolving which job description an interview session is about.

A session's `jd_id` points at either `job_descriptions` (a seeded role) or `student_jds`
(a JD the student pasted), told apart by `interview_sessions.is_custom_jd`. These two
lookups were private helpers in the interviews router; the evaluation worker needs them
too, so they live here rather than being duplicated or imported out of a router.
"""

from __future__ import annotations

from sqlalchemy.orm import Session

from app.models import InterviewSession, JobDescription, StudentJD


def jd_text_for(db: Session, session: InterviewSession) -> tuple[str, str]:
    """Return (jd_text, role_title) for a session."""
    if session.is_custom_jd:
        custom = db.query(StudentJD).filter(StudentJD.id == session.jd_id).first()
        return (custom.jd_text or "", custom.role_title or "") if custom else ("", "")
    jd = db.query(JobDescription).filter(JobDescription.id == session.jd_id).first()
    return (jd.jd_text or "", jd.role_title or "") if jd else ("", "")


def jd_labels(db: Session, session: InterviewSession) -> tuple[str, str]:
    """Return (company, role) for display. Falls back to "Unknown" rather than raising."""
    if session.is_custom_jd:
        custom = db.query(StudentJD).filter(StudentJD.id == session.jd_id).first()
        if custom is not None:
            return custom.company_name or "Custom", custom.role_title or "Role"
    jd = db.query(JobDescription).filter(JobDescription.id == session.jd_id).first()
    if jd is not None:
        return jd.company_name or "Unknown", jd.role_title or "Unknown"
    return "Unknown", "Unknown"

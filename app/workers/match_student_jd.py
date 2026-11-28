"""Match a student's pasted job description against their resume.

Moved out of the JD router for the same reason as the evaluation handler. It used to run
in `asyncio.create_task`, so any restart left the card showing "pending" forever.

On an LLM failure this deliberately re-raises: the worker retries with backoff rather than
writing a 0% that looks like a real assessment.
"""

from __future__ import annotations

import json

from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.core.llm import Task, complete_structured
from app.core.logging_setup import get_logger
from app.models import Student, StudentJD
from app.workers.jobs import register

log = get_logger("worker.match_student_jd")


class JDMatch(BaseModel):
    match_pct: int = Field(ge=0, le=100)
    matching_skills: list[str] = Field(default_factory=list, max_length=12)
    missing_skills: list[str] = Field(default_factory=list, max_length=12)
    advice: str = Field(default="", max_length=400)


_SYSTEM = """You match a resume against one job description on the skills actually present.

Be honest: a high percentage requires the resume to genuinely have the required skills.
An irrelevant resume scores 10-30.

Reply as JSON: {"match_pct": 0-100, "matching_skills": [...], "missing_skills": [...],
"advice": "one specific, actionable sentence"}"""


@register("match_student_jd")
async def match_student_jd(db: Session, payload: dict) -> None:
    row = db.query(StudentJD).filter(StudentJD.id == payload["student_jd_id"]).first()
    if row is None or row.status == "complete":
        return

    student = db.query(Student).filter(Student.id == row.student_id).first()
    resume_text = (student.resume_text or "").strip() if student else ""
    if not resume_text:
        row.status = "complete"
        row.match_pct = 0
        row.advice = "Upload your resume first, then submit this job description again."
        db.commit()
        return

    match = await complete_structured(
        task=Task.EXTRACTOR,
        system=_SYSTEM,
        user=(
            f"RESUME:\n{resume_text[:6000]}\n\n"
            f"JOB DESCRIPTION:\n{(row.jd_text or '')[:6000]}"
        ),
        schema=JDMatch,
        temperature=0.2,
        student_id=str(row.student_id),
    )

    row.match_pct = match.match_pct
    row.matched_skills = json.dumps(match.matching_skills)
    row.missing_skills = json.dumps(match.missing_skills)
    row.advice = match.advice
    row.status = "complete"
    db.commit()
    log.info("student_jd_matched", jd_id=str(row.id), match_pct=match.match_pct)

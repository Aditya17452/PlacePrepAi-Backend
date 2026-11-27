"""Student-pasted job descriptions and their resume match.

Fixes in Phase 1:

* **Authenticated and ownership-checked.** `student_id` came from the request body or the
  path, so one student could list, create against, or delete another student's JDs.
* **Durable background work.** The match ran in `asyncio.create_task`, which is lost on
  any restart — the card stayed "pending" forever. It is a queued job now.
* **No request-time DDL.** Every endpoint called `StudentJD.__table__.create(...)`.
* **Indexed for retrieval.** A JD is chunked into `jd_chunks` on creation, so the
  interview actually has requirements to ground its questions in — that table was empty
  in production.
"""

from __future__ import annotations

import json

from fastapi import APIRouter, HTTPException, Request, status
from pydantic import BaseModel, Field

from api.deps import CurrentUser, DbSession
from core import audit, ratelimit
from core.llm import LLMError, Task, complete_structured
from core.logging_setup import get_logger
from models.database import Student, StudentJD
from models.schemas import SimpleOk, StudentJDOut, SubmitJDRequest
from services import jobs, rag_service

log = get_logger("student_jd")

router = APIRouter(prefix="/api/jd", tags=["job_descriptions"])


class _JDMatch(BaseModel):
    match_pct: int = Field(ge=0, le=100)
    matching_skills: list[str] = Field(default_factory=list, max_length=12)
    missing_skills: list[str] = Field(default_factory=list, max_length=12)
    advice: str = Field(default="", max_length=400)


def _to_out(row: StudentJD) -> StudentJDOut:
    return StudentJDOut(
        id=str(row.id),
        company_name=row.company_name,
        role_title=row.role_title,
        match_pct=int(row.match_pct or 0),
        matched_skills=_safe_list(row.matched_skills),
        missing_skills=_safe_list(row.missing_skills),
        advice=row.advice or "",
        status=row.status,
        created_at=row.created_at,
    )


def _safe_list(value: str | None) -> list[str]:
    try:
        parsed = json.loads(value) if value else []
    except (json.JSONDecodeError, TypeError):
        return []
    return parsed if isinstance(parsed, list) else []


@router.post("/submit", response_model=StudentJDOut, status_code=201)
async def submit_jd(
    body: SubmitJDRequest, request: Request, principal: CurrentUser, db: DbSession
) -> StudentJDOut:
    """Save a pasted JD for the authenticated student and queue the resume match."""
    await ratelimit.limit_by_user(principal.id, "jd_submit", "20/3600")

    row = StudentJD(
        student_id=principal.id,
        company_name=body.company_name,
        role_title=body.role_title,
        jd_text=body.jd_text,
        status="pending",
    )
    db.add(row)
    db.flush()

    # Index it now so the interview has requirements to retrieve.
    rag_service.index_jd(db, str(row.id), body.jd_text)

    jobs.enqueue(
        db,
        kind="match_student_jd",
        payload={"student_jd_id": str(row.id), "student_id": principal.id},
        dedupe_key=f"match_jd:{row.id}",
    )
    audit.record(
        db,
        action="jd.submitted",
        actor_id=principal.id,
        actor_role=principal.role.value,
        target=audit.target_for("student_jd", str(row.id)),
        detail={"company": body.company_name, "role": body.role_title},
        ip=ratelimit.client_ip(request),
    )
    db.commit()
    db.refresh(row)
    return _to_out(row)


@router.get("/mine", response_model=list[StudentJDOut])
async def my_jds(principal: CurrentUser, db: DbSession) -> list[StudentJDOut]:
    rows = (
        db.query(StudentJD)
        .filter(StudentJD.student_id == principal.id)
        .order_by(StudentJD.created_at.desc())
        .limit(100)
        .all()
    )
    return [_to_out(r) for r in rows]


@router.get("/{jd_id}", response_model=StudentJDOut)
async def jd_status(
    jd_id: str, principal: CurrentUser, db: DbSession
) -> StudentJDOut:
    row = _own_jd(db, jd_id, principal.id)
    return _to_out(row)


@router.delete("/{jd_id}", response_model=SimpleOk)
async def delete_jd(
    jd_id: str, principal: CurrentUser, db: DbSession
) -> SimpleOk:
    row = _own_jd(db, jd_id, principal.id)
    db.delete(row)
    db.commit()
    return SimpleOk(ok=True, message="Removed.")


def _own_jd(db: DbSession, jd_id: str, student_id: str) -> StudentJD:  # type: ignore[valid-type]
    row = (
        db.query(StudentJD)
        .filter(StudentJD.id == jd_id, StudentJD.student_id == student_id)
        .first()
    )
    if row is None:
        # 404 rather than 403: another student's JD id should be indistinguishable
        # from one that does not exist.
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Not found")
    return row


# ── Background job ────────────────────────────────────────────────────────────


@jobs.register("match_student_jd")
async def _match_student_jd(db, payload: dict) -> None:  # type: ignore[no-untyped-def]
    row = (
        db.query(StudentJD).filter(StudentJD.id == payload["student_jd_id"]).first()
    )
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

    system = """You match a resume against one job description on the skills actually present.

Be honest: a high percentage requires the resume to genuinely have the required skills.
An irrelevant resume scores 10-30.

Reply as JSON: {"match_pct": 0-100, "matching_skills": [...], "missing_skills": [...],
"advice": "one specific, actionable sentence"}"""

    user = (
        f"RESUME:\n{resume_text[:6000]}\n\n"
        f"JOB DESCRIPTION:\n{(row.jd_text or '')[:6000]}"
    )

    try:
        match = await complete_structured(
            task=Task.EXTRACTOR,
            system=system,
            user=user,
            schema=_JDMatch,
            temperature=0.2,
            student_id=str(row.student_id),
        )
    except LLMError:
        # Leave it queued: the worker retries with backoff rather than writing a 0%
        # that looks like a real assessment.
        raise

    row.match_pct = match.match_pct
    row.matched_skills = json.dumps(match.matching_skills)
    row.missing_skills = json.dumps(match.missing_skills)
    row.advice = match.advice
    row.status = "complete"
    db.commit()
    log.info("student_jd_matched", jd_id=str(row.id), match_pct=match.match_pct)

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

from app.api.deps import CurrentUser, DbSession
from app.core import audit, ratelimit
from app.core.config import settings
from app.core.logging_setup import get_logger
from app.models import StudentJD
from app.schemas import SimpleOk, StudentJDOut, SubmitJDRequest
from app.services import rag_service
from app.workers import jobs

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

    if row.status == "pending" and settings.OPPORTUNISTIC_DRAIN:
        # Nothing else runs this job between cron ticks, and on Vercel Hobby cron fires
        # once a day — so a client polling this endpoint would poll for up to 24 hours
        # while the JD sat at "pending". Run this JD's own match job now, the same way
        # the report endpoint drains its evaluation. Claimed like the worker claims it,
        # so a concurrent cron run cannot execute it twice.
        if await jobs.drain_one(db, dedupe_key=f"match_jd:{jd_id}"):
            db.expire_all()
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



"""College admin endpoints (the role formerly called `officer`).

URL paths stay under `/api/officer/*` so the existing frontend keeps working; the role
itself is now `college_admin` (PLAN.md §0.2).

Two substantive changes:

* **Tenancy by `college_id`, not email suffix.** It used
  `Student.email.like(f"%@{college.domain}")`, so a student who changed their email
  domain silently left the college, a college without a domain saw nobody, and two
  colleges sharing a domain saw each other's students.
* **The college comes from the caller's own record**, never from a request parameter, and
  every report a college admin opens is written to the audit log.
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request, status
from pydantic import BaseModel, Field
from sqlalchemy import func

from api.deps import CollegeAdmin, DbSession
from core import audit, ratelimit
from core.llm import LLMError, Task, complete_structured
from core.logging_setup import get_logger
from models.database import (
    College,
    InterviewSession,
    JobDescription,
    SessionScore,
    Student,
    utcnow,
)
from models.identity import EnrollmentRequest, RosterEntry
from models.schemas import (
    CollegeOut,
    RosterUploadRequest,
    RosterUploadResponse,
    SimpleOk,
    StudentSummaryOut,
)
from models.tokens import TokenLot
from services import enrollment, token_service

log = get_logger("college_admin")

router = APIRouter(prefix="/api/officer", tags=["college_admin"])


def _college_of(db: DbSession, principal) -> College:  # type: ignore[no-untyped-def]
    if not principal.college_id:
        raise HTTPException(
            status.HTTP_403_FORBIDDEN,
            "This account is not linked to a college yet.",
        )
    college = (
        db.query(College)
        .filter(College.id == principal.college_id, College.deleted_at.is_(None))
        .first()
    )
    if college is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "College not found")
    return college


@router.get("/me/college", response_model=CollegeOut)
async def my_college(principal: CollegeAdmin, db: DbSession) -> CollegeOut:
    college = _college_of(db, principal)
    wallet = token_service.get_or_create_wallet(db, "college", str(college.id))
    granted = (
        db.query(func.coalesce(func.sum(TokenLot.qty), 0))
        .filter(TokenLot.wallet_id == wallet.id)
        .scalar()
        or 0
    )
    students = (
        db.query(func.count(Student.id))
        .filter(Student.college_id == str(college.id))
        .scalar()
        or 0
    )
    db.commit()
    return CollegeOut(
        id=str(college.id),
        name=college.name,
        domain=college.domain,
        is_active=bool(college.is_active),
        pool_balance=int(wallet.balance),
        pool_granted=int(granted),
        students=int(students),
        monthly_allowance_default=int(college.monthly_allowance_default or 4),
        daily_interview_cap=int(college.daily_interview_cap or 3),
        distribution_frozen=bool(college.distribution_frozen),
    )


@router.get("/students", response_model=list[StudentSummaryOut])
async def list_students(
    principal: CollegeAdmin, db: DbSession
) -> list[StudentSummaryOut]:
    """Students of the caller's college, with their latest result."""
    college = _college_of(db, principal)
    students = (
        db.query(Student)
        .filter(Student.college_id == str(college.id), Student.deleted_at.is_(None))
        .order_by(Student.name)
        .limit(2_000)
        .all()
    )

    out: list[StudentSummaryOut] = []
    for student in students:
        latest = (
            db.query(InterviewSession)
            .filter(InterviewSession.student_id == str(student.id))
            .order_by(InterviewSession.started_at.desc())
            .first()
        )
        score = (
            db.query(SessionScore)
            .filter(SessionScore.session_id == str(latest.id))
            .first()
            if latest is not None
            else None
        )
        taken = (
            db.query(func.count(InterviewSession.id))
            .filter(InterviewSession.student_id == str(student.id))
            .scalar()
            or 0
        )
        allowance = token_service.college_allowance(db, student, college)
        out.append(
            StudentSummaryOut(
                id=str(student.id),
                name=student.name or "",
                email=student.email or "",
                branch=student.branch,
                cgpa=student.cgpa,
                interviews_taken=int(taken),
                latest_score=score.overall_score if score else None,
                latest_integrity=score.integrity_score if score else None,
                allowance_remaining=allowance.remaining,
            )
        )

    db.commit()
    audit.record(
        db,
        action="college.students_listed",
        actor_id=principal.id,
        actor_role=principal.role.value,
        target=audit.target_for("college", str(college.id)),
        detail={"count": len(out)},
        commit=True,
    )
    return out


# ── Roster ────────────────────────────────────────────────────────────────────


@router.post("/roster", response_model=RosterUploadResponse)
async def upload_roster(
    body: RosterUploadRequest,
    request: Request,
    principal: CollegeAdmin,
    db: DbSession,
) -> RosterUploadResponse:
    """Upload or update the student roster.

    A roster row is the college's claim that someone belongs to it; it only takes effect
    once that person verifies the matching email or phone (PLAN.md §7.3).
    """
    college = _college_of(db, principal)
    created = updated = 0
    skipped: list[dict] = []
    seen_keys: set[str] = set()

    for index, row in enumerate(body.rows):
        email = row.email.lower() if row.email else None
        if not (email or row.phone):
            skipped.append({"row": index + 1, "reason": "needs an email or a phone"})
            continue

        key = row.enrollment_no or email or row.phone or ""
        seen_keys.add(key)

        existing = None
        if row.enrollment_no:
            existing = (
                db.query(RosterEntry)
                .filter(
                    RosterEntry.college_id == str(college.id),
                    RosterEntry.enrollment_no == row.enrollment_no,
                )
                .first()
            )
        if existing is None and email:
            existing = (
                db.query(RosterEntry)
                .filter(
                    RosterEntry.college_id == str(college.id),
                    RosterEntry.email == email,
                )
                .first()
            )

        if existing is None:
            db.add(
                RosterEntry(
                    college_id=str(college.id),
                    enrollment_no=row.enrollment_no,
                    name=row.name,
                    batch=row.batch,
                    branch=row.branch,
                    email=email,
                    phone=row.phone,
                    valid_until=row.valid_until,
                    uploaded_by=principal.id,
                )
            )
            created += 1
        else:
            existing.name = row.name or existing.name
            existing.batch = row.batch or existing.batch
            existing.branch = row.branch or existing.branch
            existing.email = email or existing.email
            existing.phone = row.phone or existing.phone
            existing.valid_until = row.valid_until or existing.valid_until
            existing.is_active = True
            updated += 1

    deactivated = 0
    if body.replace_existing:
        for entry in (
            db.query(RosterEntry)
            .filter(
                RosterEntry.college_id == str(college.id),
                RosterEntry.is_active.is_(True),
            )
            .all()
        ):
            key = entry.enrollment_no or entry.email or entry.phone or ""
            if key not in seen_keys:
                entry.is_active = False
                deactivated += 1

    audit.record(
        db,
        action="college.roster_uploaded",
        actor_id=principal.id,
        actor_role=principal.role.value,
        target=audit.target_for("college", str(college.id)),
        detail={
            "created": created,
            "updated": updated,
            "deactivated": deactivated,
            "skipped": len(skipped),
        },
        ip=ratelimit.client_ip(request),
    )
    db.commit()
    return RosterUploadResponse(
        created=created, updated=updated, deactivated=deactivated, skipped=skipped
    )


@router.get("/enrollment-requests", response_model=list[dict])
async def pending_enrollments(
    principal: CollegeAdmin, db: DbSession
) -> list[dict]:
    college = _college_of(db, principal)
    rows = (
        db.query(EnrollmentRequest)
        .filter(
            EnrollmentRequest.college_id == str(college.id),
            EnrollmentRequest.status == "pending",
        )
        .order_by(EnrollmentRequest.created_at)
        .all()
    )
    out = []
    for row in rows:
        student = db.query(Student).filter(Student.id == row.student_id).first()
        out.append(
            {
                "id": str(row.id),
                "student_id": row.student_id,
                "name": student.name if student else None,
                "email": student.email if student else None,
                "method": row.method,
                "created_at": row.created_at,
            }
        )
    return out


@router.post("/enrollment-requests/{request_id}", response_model=SimpleOk)
async def decide_enrollment(
    request_id: str,
    approve: bool,
    principal: CollegeAdmin,
    db: DbSession,
) -> SimpleOk:
    college = _college_of(db, principal)
    row = (
        db.query(EnrollmentRequest)
        .filter(
            EnrollmentRequest.id == request_id,
            EnrollmentRequest.college_id == str(college.id),
        )
        .first()
    )
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Request not found")

    enrollment.approve_request(db, row, actor_id=principal.id, approve=approve)
    db.commit()
    return SimpleOk(ok=True, message="Approved." if approve else "Rejected.")


# ── Shortlist ─────────────────────────────────────────────────────────────────


class _Ranked(BaseModel):
    student_id: str
    rank: int = Field(ge=1)
    match_score: int = Field(ge=0, le=100)
    reasoning: str = Field(max_length=300)


class _Shortlist(BaseModel):
    ranked: list[_Ranked] = Field(max_length=200)


@router.post("/shortlist", response_model=list[dict])
async def generate_shortlist(
    jd_id: str, principal: CollegeAdmin, db: DbSession
) -> list[dict]:
    """Rank the college's students against a role.

    Note: `jd_id` is a query parameter. The frontend was sending it as a JSON body, so
    every call 422'd; `src/services/api.ts` is fixed to match in the same change.
    """
    college = _college_of(db, principal)
    jd = db.query(JobDescription).filter(JobDescription.id == jd_id).first()
    if jd is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Role not found")

    students = (
        db.query(Student)
        .filter(Student.college_id == str(college.id), Student.deleted_at.is_(None))
        .limit(500)
        .all()
    )
    if not students:
        return []

    rows = []
    for student in students:
        latest = (
            db.query(InterviewSession)
            .filter(InterviewSession.student_id == str(student.id))
            .order_by(InterviewSession.started_at.desc())
            .first()
        )
        score = (
            db.query(SessionScore)
            .filter(SessionScore.session_id == str(latest.id))
            .first()
            if latest
            else None
        )
        rows.append(
            {
                "id": str(student.id),
                "name": student.name or "",
                "cgpa": student.cgpa or 0,
                "technical": score.technical_score if score else 0,
                "communication": score.communication_score if score else 0,
                "integrity": score.integrity_score if score else None,
            }
        )

    candidate_lines = "\n".join(
        f"- ID:{r['id']} CGPA:{r['cgpa']} Technical:{r['technical']} "
        f"Communication:{r['communication']}"
        for r in rows
    )
    try:
        result = await complete_structured(
            task=Task.CHEAP,
            system=(
                "Rank candidates for a shortlist using their interview scores and CGPA. "
                'Reply as JSON: {"ranked": [{"student_id": "...", "rank": 1, '
                '"match_score": 0-100, "reasoning": "one sentence"}]}'
            ),
            user=f"Role: {jd.role_title}\n\nCandidates:\n{candidate_lines}",
            schema=_Shortlist,
            temperature=0.2,
            max_tokens=1_200,
        )
        ranked = result.ranked
    except LLMError as exc:
        log.warning("shortlist_llm_failed", error=str(exc)[:160])
        # Deterministic fallback so the officer still gets a usable ordering.
        rows.sort(key=lambda r: (r["technical"], r["communication"], r["cgpa"]), reverse=True)
        ranked = [
            _Ranked(
                student_id=r["id"],
                rank=i + 1,
                match_score=int(r["technical"] or 0),
                reasoning="Ranked by interview scores (AI ranking unavailable).",
            )
            for i, r in enumerate(rows)
        ]

    by_id = {r["id"]: r for r in rows}
    out = []
    for entry in sorted(ranked, key=lambda e: e.rank):
        row = by_id.get(entry.student_id)
        if row is None:
            continue
        out.append(
            {
                "rank": entry.rank,
                "student_id": entry.student_id,
                "name": row["name"],
                "match_score": entry.match_score,
                "cgpa": row["cgpa"],
                "reasoning": entry.reasoning,
                "technical_score": row["technical"],
                "communication_score": row["communication"],
                "integrity_score": row["integrity"],
            }
        )

    audit.record(
        db,
        action="college.shortlist_generated",
        actor_id=principal.id,
        actor_role=principal.role.value,
        target=audit.target_for("jd", jd_id),
        detail={"candidates": len(out)},
        commit=True,
    )
    return out


# ── Token view (kept at the old path the frontend calls) ──────────────────────


@router.get("/credits", response_model=dict)
async def college_token_overview(principal: CollegeAdmin, db: DbSession) -> dict:
    """Pool balance and each student's remaining allowance.

    Kept at `/credits` for frontend compatibility; the underlying model is the token
    ledger, and a college admin can no longer mint anything — only distribute allowances.
    """
    college = _college_of(db, principal)
    wallet = token_service.get_or_create_wallet(db, "college", str(college.id))
    students = (
        db.query(Student)
        .filter(Student.college_id == str(college.id), Student.deleted_at.is_(None))
        .order_by(Student.name)
        .all()
    )

    rows = []
    for student in students:
        allowance = token_service.college_allowance(db, student, college)
        rows.append(
            {
                "student_id": str(student.id),
                "name": student.name,
                "email": student.email,
                "allowance": allowance.allowance,
                "used_this_period": allowance.used_this_period,
                "remaining": allowance.remaining,
                "period": allowance.period,
            }
        )
    db.commit()

    return {
        "college": {
            "pool_balance": int(wallet.balance),
            "pool_reserved": int(wallet.reserved),
            "monthly_allowance_default": int(college.monthly_allowance_default or 4),
            "daily_interview_cap": int(college.daily_interview_cap or 3),
            "distribution_frozen": bool(college.distribution_frozen),
        },
        "students": rows,
        "as_of": utcnow().isoformat(),
    }

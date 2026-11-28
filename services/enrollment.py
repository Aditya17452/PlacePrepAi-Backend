"""Proving that a student belongs to a college (PLAN.md §7.3).

The previous system treated an email suffix as proof: register with anything ending in
`@indoreinstitute.com` and you were in, which also meant alumni kept access forever and a
lookalike domain was a free pass.

Here, enrolment is only ever attempted against a **verified** contact — an email address
or phone number the student has proved they control by entering a one-time code — and the
college must independently claim them, by roster row or by a domain whose ownership an
admin has verified. A join code never settles it on its own.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from sqlalchemy import or_
from sqlalchemy.orm import Session

from core import audit
from core.config import settings
from core.logging_setup import get_logger
from models.database import College, Student, utcnow
from models.identity import EnrollmentRequest, JoinCode, RosterEntry
from models.tokens import TokenLot
from services import token_service

log = get_logger("enrollment")

Method = Literal["roster", "domain", "join_code", "none"]


@dataclass(frozen=True)
class EnrollmentResult:
    enrolled: bool
    college_id: str | None
    method: Method
    pending_approval: bool = False
    message: str = ""


def attempt_enrollment(
    db: Session, student: Student, *, join_code: str | None = None
) -> EnrollmentResult:
    """Try to attach a student to a college. Caller commits.

    Order of preference is roster, then verified domain, then join code — the same order
    as PLAN.md §7.3, because a roster row is the only signal the college itself authored
    per-student.
    """
    if not (student.is_email_verified or student.is_phone_verified):
        return EnrollmentResult(
            enrolled=False,
            college_id=None,
            method="none",
            message="Verify your email or phone number first.",
        )

    roster = _match_roster(db, student)
    if roster is not None:
        return _attach(db, student, str(roster.college_id), "roster", roster=roster)

    domain_college = _match_verified_domain(db, student)
    if domain_college is not None:
        return _attach(db, student, str(domain_college.id), "domain")

    if join_code:
        return _use_join_code(db, student, join_code)

    return EnrollmentResult(
        enrolled=False,
        college_id=None,
        method="none",
        message="You are set up as a direct student. "
        "Ask your placement officer to add you to the college roster if you expected "
        "college-funded interviews.",
    )


def _match_roster(db: Session, student: Student) -> RosterEntry | None:
    """Find an active roster row matching a contact the student has verified.

    Only verified contacts are considered: an unverified email is just a string someone
    typed.
    """
    conditions = []
    if student.is_email_verified and student.email:
        conditions.append(RosterEntry.email == student.email.lower())
    if student.is_phone_verified and student.phone:
        conditions.append(RosterEntry.phone == student.phone)
    if not conditions:
        return None

    now = utcnow()
    return (
        db.query(RosterEntry)
        .filter(or_(*conditions))
        .filter(RosterEntry.is_active.is_(True))
        .filter(
            or_(RosterEntry.valid_until.is_(None), RosterEntry.valid_until >= now)
        )
        .filter(
            or_(
                RosterEntry.claimed_by.is_(None),
                RosterEntry.claimed_by == str(student.id),
            )
        )
        .first()
    )


def _match_verified_domain(db: Session, student: Student) -> College | None:
    """Exact match against a college domain whose ownership an admin verified.

    Exact only — `indoreinstitute-edu.com` must never match `indoreinstitute.com`, and
    students can never add a domain themselves.
    """
    if not (student.is_email_verified and student.email and "@" in student.email):
        return None
    domain = student.email.rsplit("@", 1)[-1].lower()
    return (
        db.query(College)
        .filter(College.domain == domain)
        .filter(College.domain_verified_at.isnot(None))
        .filter(College.is_active.is_(True))
        .filter(College.deleted_at.is_(None))
        .first()
    )


def _use_join_code(db: Session, student: Student, code: str) -> EnrollmentResult:
    now = utcnow()
    join = (
        db.query(JoinCode)
        .filter(JoinCode.code == code.strip())
        .filter(JoinCode.is_active.is_(True))
        .first()
    )
    if join is None or (join.expires_at is not None and join.expires_at < now):
        return EnrollmentResult(
            enrolled=False,
            college_id=None,
            method="none",
            message="That join code is not valid.",
        )
    if join.max_uses is not None and int(join.uses) >= int(join.max_uses):
        return EnrollmentResult(
            enrolled=False,
            college_id=None,
            method="none",
            message="That join code has been used too many times.",
        )

    # A code alone is never enough — codes end up in group chats.
    if join.requires == "roster_match":
        return EnrollmentResult(
            enrolled=False,
            college_id=None,
            method="none",
            message="This college requires you to be on its student roster. "
            "Ask your placement officer to add you.",
        )

    existing = (
        db.query(EnrollmentRequest)
        .filter(
            EnrollmentRequest.college_id == join.college_id,
            EnrollmentRequest.student_id == str(student.id),
        )
        .first()
    )
    if existing is None:
        db.add(
            EnrollmentRequest(
                college_id=join.college_id,
                student_id=str(student.id),
                method="join_code",
                evidence={"join_code": join.code},
                status="pending",
            )
        )
    join.uses = int(join.uses) + 1

    return EnrollmentResult(
        enrolled=False,
        college_id=str(join.college_id),
        method="join_code",
        pending_approval=True,
        message="Your request was sent to the college for approval.",
    )


def _attach(
    db: Session,
    student: Student,
    college_id: str,
    method: Method,
    *,
    roster: RosterEntry | None = None,
) -> EnrollmentResult:
    student.college_id = college_id
    student.enrolled_via = method
    if roster is not None:
        roster.claimed_by = str(student.id)
        roster.claimed_at = utcnow()

    audit.record(
        db,
        action="enrollment.attached",
        actor_id=str(student.id),
        actor_role="student",
        target=audit.target_for("college", college_id),
        detail={"method": method, "roster_entry": str(roster.id) if roster else None},
    )
    log.info("student_enrolled", student_id=str(student.id), method=method)
    return EnrollmentResult(
        enrolled=True,
        college_id=college_id,
        method=method,
        message="You are enrolled with your college.",
    )


def approve_request(
    db: Session, request: EnrollmentRequest, *, actor_id: str, approve: bool
) -> None:
    """College admin decision on a pending join request. Caller commits."""
    request.status = "approved" if approve else "rejected"
    request.decided_by = actor_id
    request.decided_at = utcnow()

    if approve:
        student = db.query(Student).filter(Student.id == request.student_id).first()
        if student is not None:
            student.college_id = request.college_id
            student.enrolled_via = request.method

    audit.record(
        db,
        action="enrollment.approved" if approve else "enrollment.rejected",
        actor_id=actor_id,
        actor_role="college_admin",
        target=audit.target_for("student", request.student_id),
        detail={"college_id": request.college_id, "method": request.method},
    )


# ── Free tier ─────────────────────────────────────────────────────────────────


def grant_free_tier_if_eligible(db: Session, student: Student) -> int:
    """Grant the lifetime free tokens once both contacts are verified.

    Gated on a verified phone and granted once *per phone number*, because email is
    cheap to create and a device/phone is the practical unit of abuse.
    """
    if not (student.is_email_verified and student.is_phone_verified):
        return 0
    if not student.phone:
        return 0

    wallet = token_service.get_or_create_wallet(db, "student", str(student.id))

    # Has *this* wallet already had a free lot?
    own_free = (
        db.query(TokenLot)
        .filter(TokenLot.wallet_id == wallet.id, TokenLot.source == "free")
        .first()
    )
    if own_free is not None:
        return 0

    # Has any other account on the same verified phone already claimed it?
    sibling = (
        db.query(Student)
        .filter(Student.phone == student.phone, Student.id != student.id)
        .filter(Student.phone_verified_at.isnot(None))
        .all()
    )
    for other in sibling:
        other_wallet = token_service.get_or_create_wallet(db, "student", str(other.id))
        claimed = (
            db.query(TokenLot)
            .filter(TokenLot.wallet_id == other_wallet.id, TokenLot.source == "free")
            .first()
        )
        if claimed is not None:
            log.info(
                "free_tier_denied_duplicate_phone", student_id=str(student.id)
            )
            return 0

    token_service.grant(
        db,
        owner_type="student",
        owner_id=str(student.id),
        qty=settings.FREE_TIER_TOKENS,
        reason="Free tier: verified email and phone",
        actor_id=None,
        validity_days=None,
        source="free",
    )
    return settings.FREE_TIER_TOKENS

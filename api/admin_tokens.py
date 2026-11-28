"""Token balances, admin grants, and college allowance distribution (PLAN.md §8).

Who may do what:

* A **student** reads their own wallet and ledger. Nothing else.
* A **college admin** reads their own college's pool and sets allowances — which are caps
  on drawing from the pool, not transfers. They cannot create tokens.
* Only an **admin** brings tokens into existence, and every grant and revoke needs a
  reason that lands in both the ledger and the audit log.
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request, status
from sqlalchemy import func

from api.deps import Admin, CollegeAdmin, CurrentUser, DbSession
from core import audit, ratelimit
from core.config import settings
from core.logging_setup import get_logger
from models.database import College, Student
from models.schemas import (
    AllowanceGroupRequest,
    AllowanceOverrideRequest,
    CollegeOut,
    GrantTokensRequest,
    LedgerEntryOut,
    RevokeTokensRequest,
    SimpleOk,
    WalletResponse,
)
from models.tokens import (
    AllowanceGroup,
    AllowanceMember,
    AllowanceOverride,
    TokenLedger,
    TokenLot,
)
from services import token_service

log = get_logger("tokens_api")

router = APIRouter(tags=["tokens"])


# ── Student: own wallet ───────────────────────────────────────────────────────


@router.get("/api/tokens/wallet", response_model=WalletResponse)
async def my_wallet(principal: CurrentUser, db: DbSession) -> WalletResponse:
    student = db.query(Student).filter(Student.id == principal.id).first()
    if student is None:  # pragma: no cover
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Not found")

    avail = token_service.availability(db, student)
    wallet = token_service.get_or_create_wallet(db, "student", principal.id)

    next_lot = (
        db.query(TokenLot)
        .filter(
            TokenLot.wallet_id == wallet.id,
            TokenLot.remaining > 0,
            TokenLot.expires_at.isnot(None),
        )
        .order_by(TokenLot.expires_at.asc())
        .first()
    )
    db.commit()

    return WalletResponse(
        personal_balance=avail.personal_balance,
        personal_reserved=avail.personal_reserved,
        college_allowance_remaining=avail.college_allowance_remaining,
        college_pool_remaining=avail.college_pool_remaining,
        total_available=avail.total_available,
        next_expiry=next_lot.expires_at if next_lot else None,
        next_expiry_qty=int(next_lot.remaining) if next_lot else None,
    )


@router.get("/api/tokens/ledger", response_model=list[LedgerEntryOut])
async def my_ledger(
    principal: CurrentUser, db: DbSession, limit: int = 50
) -> list[LedgerEntryOut]:
    """The caller's own token history.

    Includes spends funded by their college, which live on the college's wallet — matched
    by `on_behalf_of` so a student can see them without being able to read the pool.
    """
    wallet = token_service.get_or_create_wallet(db, "student", principal.id)
    db.commit()

    rows = (
        db.query(TokenLedger)
        .filter(
            (TokenLedger.wallet_id == wallet.id)
            | (TokenLedger.on_behalf_of == principal.id)
        )
        .order_by(TokenLedger.created_at.desc())
        .limit(min(max(limit, 1), 200))
        .all()
    )
    return [LedgerEntryOut.model_validate(r) for r in rows]


# ── Admin: creating and removing tokens ───────────────────────────────────────


@router.post("/api/admin/tokens/grant", response_model=SimpleOk)
async def grant_college_tokens(
    body: GrantTokensRequest, request: Request, principal: Admin, db: DbSession
) -> SimpleOk:
    college = db.query(College).filter(College.id == body.college_id).first()
    if college is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "College not found")

    lot = token_service.grant(
        db,
        owner_type="college",
        owner_id=str(college.id),
        qty=body.tokens,
        reason=body.reason,
        actor_id=principal.id,
        validity_days=body.validity_days or settings.COLLEGE_POOL_VALIDITY_DAYS,
    )
    audit.record(
        db,
        action="tokens.granted",
        actor_id=principal.id,
        actor_role=principal.role.value,
        target=audit.target_for("college", str(college.id)),
        detail={"tokens": body.tokens, "reason": body.reason, "lot_id": lot.id},
        ip=ratelimit.client_ip(request),
    )
    db.commit()
    return SimpleOk(
        ok=True, message=f"Granted {body.tokens} tokens to {college.name}."
    )


@router.post("/api/admin/tokens/revoke", response_model=SimpleOk)
async def revoke_college_tokens(
    body: RevokeTokensRequest, request: Request, principal: Admin, db: DbSession
) -> SimpleOk:
    college = db.query(College).filter(College.id == body.college_id).first()
    if college is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "College not found")

    taken = token_service.revoke(
        db,
        owner_type="college",
        owner_id=str(college.id),
        qty=body.tokens,
        reason=body.reason,
        actor_id=principal.id,
    )
    audit.record(
        db,
        action="tokens.revoked",
        actor_id=principal.id,
        actor_role=principal.role.value,
        target=audit.target_for("college", str(college.id)),
        detail={"requested": body.tokens, "revoked": taken, "reason": body.reason},
        ip=ratelimit.client_ip(request),
    )
    db.commit()

    if taken < body.tokens:
        return SimpleOk(
            ok=True,
            message=f"Revoked {taken} of {body.tokens}; the rest are already spent "
            "or reserved and cannot be taken back.",
        )
    return SimpleOk(ok=True, message=f"Revoked {taken} tokens.")


# ── College admin: the pool and its distribution ──────────────────────────────


def _own_college(db: DbSession, principal) -> College:  # type: ignore[no-untyped-def]
    """Resolve the caller's college from the database, never from the request."""
    if principal.college_id is None:
        raise HTTPException(
            status.HTTP_403_FORBIDDEN,
            "This account is not linked to a college.",
        )
    college = db.query(College).filter(College.id == principal.college_id).first()
    if college is None or college.deleted_at is not None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "College not found")
    return college


@router.get("/api/college/pool", response_model=CollegeOut)
async def college_pool(principal: CollegeAdmin, db: DbSession) -> CollegeOut:
    college = _own_college(db, principal)
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


@router.post("/api/college/allowance-groups", response_model=SimpleOk)
async def create_allowance_group(
    body: AllowanceGroupRequest,
    request: Request,
    principal: CollegeAdmin,
    db: DbSession,
) -> SimpleOk:
    """Create an allowance group from roster filters and populate its membership.

    This caps how much of the pool each member may draw; it moves no tokens, so the sum
    of allowances across groups may exceed the pool.
    """
    college = _own_college(db, principal)
    if college.distribution_frozen:
        raise HTTPException(
            status.HTTP_403_FORBIDDEN,
            "Token distribution for this college is frozen. Contact PlacePrep support.",
        )

    group = AllowanceGroup(
        college_id=str(college.id),
        name=body.name,
        filters=body.filters,
        per_student_allowance=body.per_student_allowance,
        period=body.period,
        valid_from=body.valid_from,
        valid_until=body.valid_until,
        created_by=principal.id,
    )
    db.add(group)
    db.flush()

    members = _students_matching(db, college, body.filters)
    for student_id in members:
        db.add(AllowanceMember(group_id=group.id, student_id=student_id))

    audit.record(
        db,
        action="allowance.group_created",
        actor_id=principal.id,
        actor_role=principal.role.value,
        target=audit.target_for("college", str(college.id)),
        detail={
            "group": body.name,
            "per_student": body.per_student_allowance,
            "period": body.period,
            "members": len(members),
        },
        ip=ratelimit.client_ip(request),
    )
    db.commit()
    return SimpleOk(
        ok=True,
        message=f"Group '{body.name}' created with {len(members)} students at "
        f"{body.per_student_allowance} interviews per {body.period}.",
    )


@router.post("/api/college/allowance-overrides", response_model=SimpleOk)
async def create_allowance_override(
    body: AllowanceOverrideRequest,
    request: Request,
    principal: CollegeAdmin,
    db: DbSession,
) -> SimpleOk:
    """An individual top-up, or a time-boxed drive allowance."""
    college = _own_college(db, principal)
    if college.distribution_frozen:
        raise HTTPException(
            status.HTTP_403_FORBIDDEN, "Token distribution for this college is frozen."
        )

    target = (
        db.query(Student)
        .filter(Student.id == body.student_id, Student.college_id == str(college.id))
        .first()
    )
    if target is None:
        # 404 rather than 403, so this cannot be used to test whether an ID exists
        # at another college.
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Student not found")

    db.add(
        AllowanceOverride(
            college_id=str(college.id),
            student_id=body.student_id,
            kind=body.kind,
            extra_allowance=body.extra_allowance,
            label=body.label,
            reason=body.reason,
            jd_id=body.jd_id,
            valid_from=body.valid_from,
            valid_until=body.valid_until,
            created_by=principal.id,
        )
    )
    audit.record(
        db,
        action="allowance.override_created",
        actor_id=principal.id,
        actor_role=principal.role.value,
        target=audit.target_for("student", body.student_id),
        detail={
            "kind": body.kind,
            "extra": body.extra_allowance,
            "reason": body.reason,
            "label": body.label,
        },
        ip=ratelimit.client_ip(request),
    )
    db.commit()
    return SimpleOk(
        ok=True,
        message=f"Added {body.extra_allowance} extra interviews for {target.name}.",
    )


def _students_matching(
    db: DbSession, college: College, filters: dict  # type: ignore[valid-type]
) -> list[str]:
    """Resolve roster filters to student IDs at this college.

    Filters are matched against the student record, and only ever within the caller's own
    college — a filter can never widen the scope.
    """
    query = db.query(Student.id).filter(Student.college_id == str(college.id))
    branch = filters.get("branch")
    if branch:
        query = query.filter(Student.branch == branch)
    batch = filters.get("batch")
    if batch:
        try:
            query = query.filter(Student.year_of_passing == int(batch))
        except (TypeError, ValueError):
            raise HTTPException(
                status.HTTP_400_BAD_REQUEST, "batch must be a year, e.g. 2027"
            ) from None
    return [row[0] for row in query.all()]

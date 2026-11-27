"""Platform admin endpoints (the role formerly called `super_admin`).

URL paths stay under `/api/super/*` for frontend compatibility; the role is now `admin`.

The important change is college enrolment. It used to accept a `handler_password` in the
request and set it on the officer's account — we were choosing someone else's password and
transmitting it through our own logs. Enrolment now creates the college and **invites**
the college admin, who sets their own password through a one-time link.
"""

from __future__ import annotations

from datetime import timedelta

from fastapi import APIRouter, HTTPException, Request, status
from sqlalchemy import func

from api.deps import Admin, DbSession
from core import audit, ratelimit
from core.config import settings
from core.logging_setup import get_logger
from core.roles import Role, normalize_role
from core.security import hash_password
from models.billing import LlmUsage, Payment
from models.database import College, InterviewSession, Student, utcnow
from models.identity import ApiAuditLog
from models.schemas import (
    AcceptInviteRequest,
    AuditLogOut,
    CollegeOut,
    EnrollCollegeRequest,
    LedgerEntryOut,
    PlatformStatsResponse,
    SimpleOk,
    TokenResponse,
)
from models.tokens import TokenLedger, TokenLot
from services import notify, token_service, verification

log = get_logger("admin")

router = APIRouter(prefix="/api/super", tags=["admin"])


# ── Stats ─────────────────────────────────────────────────────────────────────


@router.get("/stats", response_model=PlatformStatsResponse)
async def platform_stats(principal: Admin, db: DbSession) -> PlatformStatsResponse:
    total_students = (
        db.query(func.count(Student.id)).filter(Student.role == "student").scalar() or 0
    )
    total_colleges = (
        db.query(func.count(College.id))
        .filter(College.is_active.is_(True), College.deleted_at.is_(None))
        .scalar()
        or 0
    )
    total_interviews = db.query(func.count(InterviewSession.id)).scalar() or 0
    completed = (
        db.query(func.count(InterviewSession.id))
        .filter(InterviewSession.status == "completed")
        .scalar()
        or 0
    )

    outstanding = (
        db.query(func.coalesce(func.sum(TokenLot.remaining), 0)).scalar() or 0
    )
    consumed = (
        db.query(func.coalesce(func.sum(-TokenLedger.amount), 0))
        .filter(TokenLedger.kind == "consume")
        .scalar()
        or 0
    )
    revenue = (
        db.query(func.coalesce(func.sum(Payment.amount_paise), 0))
        .filter(Payment.status == "captured")
        .scalar()
        or 0
    )

    since = utcnow() - timedelta(days=30)
    llm_cost = (
        db.query(func.coalesce(func.sum(LlmUsage.cost_usd), 0.0))
        .filter(LlmUsage.created_at >= since)
        .scalar()
        or 0.0
    )
    interviews_30d = (
        db.query(func.count(InterviewSession.id))
        .filter(InterviewSession.started_at >= since)
        .scalar()
        or 0
    )

    # The §9 guardrail: hold the Rs 10 and Rs 9 college tiers until this is under Rs 5.
    cost_per_interview = None
    if interviews_30d:
        # Approximate USD->INR; refine once there is a billing feed to reconcile against.
        cost_per_interview = round((float(llm_cost) * 84.0) / interviews_30d, 2)

    return PlatformStatsResponse(
        total_students=int(total_students),
        total_colleges=int(total_colleges),
        total_interviews=int(total_interviews),
        completed_interviews=int(completed),
        tokens_outstanding=int(outstanding),
        tokens_consumed=int(consumed),
        revenue_paise=int(revenue),
        cost_per_interview_inr=cost_per_interview,
        llm_cost_usd_30d=round(float(llm_cost), 4),
    )


# ── Colleges ──────────────────────────────────────────────────────────────────


@router.get("/colleges", response_model=list[CollegeOut])
async def list_colleges(principal: Admin, db: DbSession) -> list[CollegeOut]:
    colleges = (
        db.query(College)
        .filter(College.deleted_at.is_(None))
        .order_by(College.name)
        .all()
    )
    out: list[CollegeOut] = []
    for college in colleges:
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
        out.append(
            CollegeOut(
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
        )
    db.commit()
    return out


@router.post("/colleges/enroll", response_model=SimpleOk, status_code=201)
async def enroll_college(
    body: EnrollCollegeRequest, request: Request, principal: Admin, db: DbSession
) -> SimpleOk:
    """Create a college and invite its admin.

    No tokens are granted here — that is a separate, audited action with a reason
    (`POST /api/admin/tokens/grant`), so a pool can never appear without a record of who
    created it and why.
    """
    admin_email = body.admin_email.strip().lower()
    domain = body.domain.strip().lower().lstrip("@") if body.domain else None

    if domain:
        clash = db.query(College).filter(College.domain == domain).first()
        if clash is not None:
            raise HTTPException(
                status.HTTP_409_CONFLICT, f"Domain '{domain}' is already in use."
            )

    college = College(
        name=body.name.strip(),
        # Stored but NOT verified: a domain only enrolls students once an admin has
        # confirmed the college owns it (PLAN.md §7.3).
        domain=domain,
        domain_verified_at=None,
        monthly_allowance_default=body.monthly_allowance_default,
        daily_interview_cap=body.daily_interview_cap,
        is_active=True,
    )
    db.add(college)
    db.flush()
    token_service.get_or_create_wallet(db, "college", str(college.id))

    account = db.query(Student).filter(Student.email == admin_email).first()
    if account is None:
        account = Student(
            name=body.admin_name.strip(),
            email=admin_email,
            role=Role.COLLEGE_ADMIN.value,
            # No password is set by us. The invite link lets them choose one.
            password_hash=None,
            password_algo="invite_pending",
            branch="Administration",
            cgpa=0.0,
            college_id=str(college.id),
            is_active=True,
            enrolled_via="direct",
        )
        db.add(account)
        db.flush()
    else:
        account.role = Role.COLLEGE_ADMIN.value
        account.college_id = str(college.id)
        account.token_version = int(getattr(account, "token_version", 0) or 0) + 1

    college.handler_email = admin_email
    college.handler_id = str(account.id)

    invite = verification.issue(
        db,
        purpose="admin_invite",
        subject=admin_email,
        student_id=str(account.id),
        ttl_sec=7 * 24 * 3600,
        numeric=False,
        request_ip=ratelimit.client_ip(request),
    )
    audit.record(
        db,
        action="admin.college_enrolled",
        actor_id=principal.id,
        actor_role=principal.role.value,
        target=audit.target_for("college", str(college.id)),
        detail={"name": college.name, "admin_email": admin_email, "domain": domain},
        ip=ratelimit.client_ip(request),
    )
    db.commit()

    try:
        await notify.send_email_code(
            to=admin_email, code=invite.code, purpose="admin_invite"
        )
        delivered = True
    except notify.NotificationUnavailable as exc:
        log.error("invite_not_delivered", error=str(exc)[:160])
        delivered = False

    message = f"College '{college.name}' created and {admin_email} invited."
    if not delivered:
        message += (
            " The invite email could not be sent — no email provider is configured yet."
        )
    return SimpleOk(ok=True, message=message)


@router.post("/colleges/{college_id}/deactivate", response_model=SimpleOk)
async def deactivate_college(
    college_id: str, request: Request, principal: Admin, db: DbSession
) -> SimpleOk:
    """Soft-delete. Hard deletion is a separate purge job after the retention period."""
    college = db.query(College).filter(College.id == college_id).first()
    if college is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "College not found")

    college.is_active = False
    audit.record(
        db,
        action="admin.college_deactivated",
        actor_id=principal.id,
        actor_role=principal.role.value,
        target=audit.target_for("college", college_id),
        detail={"name": college.name},
        ip=ratelimit.client_ip(request),
    )
    db.commit()
    return SimpleOk(ok=True, message=f"{college.name} deactivated.")


@router.post("/colleges/{college_id}/verify-domain", response_model=SimpleOk)
async def verify_domain(
    college_id: str, request: Request, principal: Admin, db: DbSession
) -> SimpleOk:
    """Mark a college's domain as verified, enabling domain-based enrolment.

    Deliberately a manual admin action: the check is a DNS TXT record or written
    confirmation from the college, done out of band. Students can never self-add a domain,
    which is what stops a lookalike like `indoreinstitute-edu.com`.
    """
    college = db.query(College).filter(College.id == college_id).first()
    if college is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "College not found")
    if not college.domain:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST, "This college has no domain configured."
        )

    college.domain_verified_at = utcnow()
    audit.record(
        db,
        action="admin.domain_verified",
        actor_id=principal.id,
        actor_role=principal.role.value,
        target=audit.target_for("college", college_id),
        detail={"domain": college.domain},
        ip=ratelimit.client_ip(request),
    )
    db.commit()
    return SimpleOk(ok=True, message=f"{college.domain} marked verified.")


@router.post("/colleges/{college_id}/freeze", response_model=SimpleOk)
async def freeze_distribution(
    college_id: str, frozen: bool, principal: Admin, db: DbSession
) -> SimpleOk:
    college = db.query(College).filter(College.id == college_id).first()
    if college is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "College not found")
    college.distribution_frozen = frozen
    audit.record(
        db,
        action="admin.distribution_frozen" if frozen else "admin.distribution_unfrozen",
        actor_id=principal.id,
        actor_role=principal.role.value,
        target=audit.target_for("college", college_id),
    )
    db.commit()
    return SimpleOk(ok=True)


# ── Invites ───────────────────────────────────────────────────────────────────


@router.post("/invite/accept", response_model=TokenResponse)
async def accept_invite(
    body: AcceptInviteRequest, request: Request, db: DbSession
) -> TokenResponse:
    """A college admin sets their own password from the invite link."""
    await ratelimit.limit_auth(request)
    try:
        row = verification.consume_token(db, purpose="admin_invite", code=body.code)
    except verification.VerificationError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from exc

    account = db.query(Student).filter(Student.id == row.student_id).first()
    if account is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Account not found")

    account.password_hash = hash_password(body.password)
    account.password_algo = "bcrypt"
    account.email_verified_at = account.email_verified_at or utcnow()
    account.token_version = int(getattr(account, "token_version", 0) or 0) + 1

    audit.record(
        db,
        action="admin.invite_accepted",
        actor_id=str(account.id),
        actor_role=normalize_role(account.role).value,
        ip=ratelimit.client_ip(request),
    )
    db.commit()
    db.refresh(account)

    from core.security import make_access_token

    return TokenResponse(
        token=make_access_token(
            str(account.id), int(getattr(account, "token_version", 0) or 0)
        ),
        role=normalize_role(account.role).value,  # type: ignore[arg-type]
        name=account.name or "",
        id=str(account.id),
    )


# ── Audit & ledger ────────────────────────────────────────────────────────────


@router.get("/audit-log", response_model=list[AuditLogOut])
async def audit_log(
    principal: Admin, db: DbSession, limit: int = 100
) -> list[AuditLogOut]:
    rows = (
        db.query(ApiAuditLog)
        .order_by(ApiAuditLog.created_at.desc())
        .limit(min(max(limit, 1), 500))
        .all()
    )
    return [AuditLogOut.model_validate(r) for r in rows]


@router.get("/credit-logs", response_model=list[LedgerEntryOut])
async def token_ledger(
    principal: Admin, db: DbSession, limit: int = 100
) -> list[LedgerEntryOut]:
    """Every token movement, newest first. Kept at the old path for the admin panel."""
    rows = (
        db.query(TokenLedger)
        .order_by(TokenLedger.created_at.desc())
        .limit(min(max(limit, 1), 500))
        .all()
    )
    return [LedgerEntryOut.model_validate(r) for r in rows]


# ── Maintenance (called by cron) ──────────────────────────────────────────────


@router.post("/jobs/drain", response_model=dict, include_in_schema=False)
async def drain_jobs(principal: Admin, db: DbSession, limit: int = 5) -> dict:
    """Run queued background jobs.

    Exposed to admins as well as cron so a stuck queue can be nudged by hand instead of
    waiting for the next scheduled run.
    """
    from services import jobs

    jobs.requeue_stale(db)
    result = await jobs.drain(db, limit=limit)
    return result


@router.get("/config-check", response_model=dict)
async def config_check(principal: Admin) -> dict:
    """What is and is not configured, for the launch checklist.

    Reports presence only — never a secret's value.
    """
    return {
        "env": settings.ENV,
        "database": bool(settings.DATABASE_URL),
        "redis": settings.redis_configured,
        "groq": bool(settings.GROQ_API_KEY),
        "groq_fallback_key": bool(settings.GROQ_API_KEY2),
        "question_model": settings.LLM_MODEL_QUESTION,
        "stt_model": settings.STT_MODEL,
        "google_oauth": bool(settings.GOOGLE_CLIENT_ID),
        "razorpay": bool(settings.RAZORPAY_KEY_ID),
        "razorpay_webhook": bool(settings.RAZORPAY_WEBHOOK_SECRET),
        "razorpay_mode": "test"
        if settings.RAZORPAY_KEY_ID.startswith("rzp_test_")
        else ("live" if settings.RAZORPAY_KEY_ID else "unset"),
        "supabase_storage": bool(settings.SUPABASE_SERVICE_ROLE_KEY),
        "github_token": bool(settings.GITHUB_TOKEN),
        "email_provider": bool(settings.RESEND_API_KEY),
        "sms_provider": bool(settings.SMS_API_KEY),
        "sentry": bool(settings.SENTRY_DSN),
    }

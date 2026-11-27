"""Authentication: registration, login, verification, Google OAuth.

The three security fixes here are the reason Phase 1a ships first:

1. **Roles are assigned by the server.** `register` previously did
   `role = body.get("role", "student")`, so `{"role": "officer"}` in the request body
   granted officer access to every college-scoped endpoint. There is no role field on the
   request model at all now, and `extra="forbid"` rejects one if it is sent.
2. **Passwords are bcrypt.** They were unsalted SHA-256. Existing hashes still work for
   one more login and are re-hashed on the way through.
3. **No JWT in a redirect URL.** The Google callback issued
   `/auth/callback?token=<jwt>&role=...`, putting a live credential into browser history,
   the Referer header and every proxy log in between. The callback now redirects with a
   short-lived single-use code that the frontend POSTs back to exchange for a token.
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request, Response, status
from fastapi.responses import RedirectResponse
from sqlalchemy.orm import Session

from app.api.deps import Admin, CurrentUser, DbSession
from app.core import audit, ratelimit
from app.core.config import settings
from app.core.logging_setup import get_logger
from app.core.roles import Role, normalize_role
from app.core.security import (
    hash_password,
    make_access_token,
    verify_password,
)
from app.models import College, Student, utcnow
from app.schemas import (
    LoginRequest,
    MeResponse,
    MfaConfirmRequest,
    MfaSetupResponse,
    OAuthExchangeRequest,
    OAuthUrlResponse,
    RegisterRequest,
    SendOtpRequest,
    SimpleOk,
    TokenResponse,
    VerifyOtpRequest,
)
from app.services import enrollment, notify, token_service, verification

log = get_logger("auth")

router = APIRouter(prefix="/api/auth", tags=["auth"])

#: Domains that exist only to receive one message. Blocked for the free tier (PLAN.md §8.4).
_DISPOSABLE_DOMAINS = frozenset(
    {
        "mailinator.com",
        "guerrillamail.com",
        "10minutemail.com",
        "tempmail.com",
        "temp-mail.org",
        "yopmail.com",
        "throwawaymail.com",
        "sharklasers.com",
        "getnada.com",
        "trashmail.com",
        "dispostable.com",
        "maildrop.cc",
    }
)


def _issue(student: Student, *, needs_verification: bool = False) -> TokenResponse:
    role = normalize_role(student.role)
    return TokenResponse(
        token=make_access_token(
            str(student.id), int(getattr(student, "token_version", 0) or 0)
        ),
        role=role.value,  # type: ignore[arg-type]
        name=student.name or "",
        id=str(student.id),
        needs_verification=needs_verification,
    )


def _email_domain(email: str) -> str:
    return email.rsplit("@", 1)[-1].lower() if "@" in email else ""


# ── Register ──────────────────────────────────────────────────────────────────


@router.post("/register", response_model=TokenResponse, status_code=201)
async def register(
    body: RegisterRequest, request: Request, db: DbSession
) -> TokenResponse:
    """Create a direct student account.

    Signup is open (PLAN.md §0 decision 1): anyone may register as a direct student.
    College membership is *not* decided here — it is attempted after the contact details
    have been verified, because an email address someone typed proves nothing.
    """
    await ratelimit.limit_auth(request)

    email = body.email.strip().lower()
    if _email_domain(email) in _DISPOSABLE_DOMAINS:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            "Please use a permanent email address.",
        )

    if db.query(Student).filter(Student.email == email).first() is not None:
        # Same message as a wrong password on login would give, to avoid confirming
        # which addresses are registered.
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            "That email is already registered. Try signing in instead.",
        )

    if body.phone:
        existing_phone = (
            db.query(Student).filter(Student.phone == body.phone).first()
        )
        if existing_phone is not None:
            raise HTTPException(
                status.HTTP_409_CONFLICT,
                "That phone number is already linked to another account.",
            )

    student = Student(
        name=body.name.strip(),
        email=email,
        # Always a student. Officers and admins are created by an admin, never here.
        role=Role.STUDENT.value,
        phone=body.phone,
        cgpa=body.cgpa if body.cgpa is not None else 7.0,
        branch=body.branch or "Computer Science",
        year_of_passing=body.year_of_passing or 2026,
        password_hash=hash_password(body.password),
        password_algo="bcrypt",
        token_version=0,
        is_active=True,
        enrolled_via="direct",
    )
    db.add(student)
    db.flush()

    # Personal wallet exists from day one; the free tokens arrive after verification.
    token_service.get_or_create_wallet(db, "student", str(student.id))

    if body.join_code:
        # Remembered, not honoured: a join code is only ever applied together with a
        # verified contact and a roster match or an approval (PLAN.md §7.3).
        student.skills = student.skills or []

    audit.record(
        db,
        action="auth.register",
        actor_id=str(student.id),
        actor_role=Role.STUDENT.value,
        target=audit.target_for("student", str(student.id)),
        detail={"enrolled_via": "direct"},
        ip=ratelimit.client_ip(request),
    )
    db.commit()
    db.refresh(student)

    await _send_email_otp(db, student, request)
    log.info("registered", student_id=str(student.id))
    return _issue(student, needs_verification=True)


# ── Login ─────────────────────────────────────────────────────────────────────


@router.post("/login", response_model=TokenResponse)
async def login(body: LoginRequest, request: Request, db: DbSession) -> TokenResponse:
    await ratelimit.limit_auth(request)

    email = body.email.strip().lower()
    student = db.query(Student).filter(Student.email == email).first()

    # One generic message for "no such account" and "wrong password", so the endpoint
    # cannot be used to enumerate registered addresses.
    invalid = HTTPException(
        status.HTTP_401_UNAUTHORIZED, "Invalid email or password."
    )
    if student is None:
        raise invalid

    check = verify_password(body.password, student.password_hash)
    if not check.ok:
        raise invalid

    if getattr(student, "is_active", True) is False:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "This account is deactivated.")

    role = normalize_role(student.role)

    # TOTP is mandatory for admins (PLAN.md §7.1).
    if getattr(student, "mfa_enabled", False):
        if not body.mfa_code:
            raise HTTPException(
                status.HTTP_401_UNAUTHORIZED, "Enter your authenticator code."
            )
        if not _verify_totp(student.mfa_secret, body.mfa_code):
            raise HTTPException(
                status.HTTP_401_UNAUTHORIZED, "That authenticator code is not correct."
            )
    elif role is Role.ADMIN:
        log.warning("admin_without_mfa", student_id=str(student.id))

    if check.needs_rehash:
        # Upgrade the legacy unsalted SHA-256 digest now that we have the plaintext.
        student.password_hash = hash_password(body.password)
        student.password_algo = "bcrypt"
        log.info("password_rehashed", student_id=str(student.id))

    db.commit()
    db.refresh(student)
    return _issue(
        student,
        needs_verification=not (
            student.is_email_verified or role in (Role.ADMIN, Role.COLLEGE_ADMIN)
        ),
    )


@router.post("/logout-all", response_model=SimpleOk)
async def logout_everywhere(principal: CurrentUser, db: DbSession) -> SimpleOk:
    """Invalidate every outstanding token for the caller by bumping the token version."""
    student = db.query(Student).filter(Student.id == principal.id).first()
    if student is None:  # pragma: no cover - principal came from this table
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Not found")
    student.token_version = int(getattr(student, "token_version", 0) or 0) + 1
    db.commit()
    return SimpleOk(ok=True, message="Signed out on all devices.")


# ── Me ────────────────────────────────────────────────────────────────────────


@router.get("/me", response_model=MeResponse)
async def me(principal: CurrentUser, db: DbSession) -> MeResponse:
    student = db.query(Student).filter(Student.id == principal.id).first()
    if student is None:  # pragma: no cover
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Not found")

    college_name = None
    if student.college_id:
        college = db.query(College).filter(College.id == student.college_id).first()
        college_name = college.name if college else None

    avail = token_service.availability(db, student)
    db.commit()  # availability may have created the wallet row

    return MeResponse(
        id=str(student.id),
        name=student.name or "",
        email=student.email or "",
        role=principal.role.value,  # type: ignore[arg-type]
        branch=student.branch,
        cgpa=student.cgpa,
        college_id=principal.college_id,
        college_name=college_name,
        email_verified=student.is_email_verified,
        phone_verified=student.is_phone_verified,
        mfa_enabled=bool(getattr(student, "mfa_enabled", False)),
        tokens_available=avail.total_available,
        personal_tokens=max(avail.personal_balance - avail.personal_reserved, 0),
        college_allowance_remaining=avail.college_allowance_remaining or 0,
    )


# ── Verification ──────────────────────────────────────────────────────────────


@router.post("/otp/send", response_model=SimpleOk)
async def send_otp(
    body: SendOtpRequest, request: Request, principal: CurrentUser, db: DbSession
) -> SimpleOk:
    student = db.query(Student).filter(Student.id == principal.id).first()
    if student is None:  # pragma: no cover
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Not found")

    if body.channel == "email":
        destination = (body.destination or student.email or "").strip().lower()
    else:
        destination = (body.destination or student.phone or "").strip()

    if not destination:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            f"No {body.channel} on file. Provide one to verify.",
        )

    # Per-destination and per-IP, so neither one number nor one attacker can be farmed.
    await ratelimit.limit_otp(request, destination)

    if body.channel == "phone":
        clash = (
            db.query(Student)
            .filter(Student.phone == destination, Student.id != student.id)
            .filter(Student.phone_verified_at.isnot(None))
            .first()
        )
        if clash is not None:
            raise HTTPException(
                status.HTTP_409_CONFLICT,
                "That phone number is already verified on another account.",
            )
        student.phone = destination

    purpose = "email_otp" if body.channel == "email" else "phone_otp"
    issued = verification.issue(
        db,
        purpose=purpose,  # type: ignore[arg-type]
        subject=destination,
        student_id=str(student.id),
        request_ip=ratelimit.client_ip(request),
    )
    db.commit()

    try:
        if body.channel == "email":
            await notify.send_email_code(
                to=destination, code=issued.code, purpose=purpose
            )
        else:
            await notify.send_sms_code(
                to=destination, code=issued.code, purpose=purpose
            )
    except notify.NotificationUnavailable as exc:
        log.error("otp_delivery_unavailable", channel=body.channel, error=str(exc))
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            f"We cannot send {body.channel} codes right now. Please try again later.",
        ) from exc

    return SimpleOk(ok=True, message="Code sent. It expires in 10 minutes.")


@router.post("/otp/verify", response_model=MeResponse)
async def verify_otp(
    body: VerifyOtpRequest, request: Request, principal: CurrentUser, db: DbSession
) -> MeResponse:
    student = db.query(Student).filter(Student.id == principal.id).first()
    if student is None:  # pragma: no cover
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Not found")

    subject = (
        (student.email or "").lower()
        if body.channel == "email"
        else (student.phone or "")
    )
    purpose = "email_otp" if body.channel == "email" else "phone_otp"

    try:
        verification.consume(
            db, purpose=purpose, subject=subject, code=body.code  # type: ignore[arg-type]
        )
    except verification.VerificationError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from exc

    now = utcnow()
    if body.channel == "email":
        student.email_verified_at = now
    else:
        student.phone_verified_at = now

    # Verified contact details are the trigger for both enrolment and the free tier.
    result = enrollment.attempt_enrollment(db, student)
    granted = enrollment.grant_free_tier_if_eligible(db, student)
    db.commit()
    db.refresh(student)

    log.info(
        "contact_verified",
        student_id=str(student.id),
        channel=body.channel,
        enrolled=result.enrolled,
        free_tokens=granted,
    )
    return await me(principal, db)  # type: ignore[arg-type]


# ── Google OAuth ──────────────────────────────────────────────────────────────


@router.get("/google", response_model=OAuthUrlResponse)
async def google_login() -> OAuthUrlResponse:
    if not settings.GOOGLE_CLIENT_ID:
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            "Google sign-in is not configured on this server.",
        )
    from urllib.parse import urlencode

    query = urlencode(
        {
            "client_id": settings.GOOGLE_CLIENT_ID,
            "redirect_uri": settings.GOOGLE_REDIRECT_URI,
            "response_type": "code",
            "scope": "openid email profile",
            "access_type": "online",
            "prompt": "select_account",
        }
    )
    return OAuthUrlResponse(
        auth_url=f"https://accounts.google.com/o/oauth2/v2/auth?{query}"
    )


@router.get("/google/callback")
async def google_callback(code: str, request: Request, db: DbSession) -> Response:
    """Exchange Google's code, then redirect with a one-time hand-off code.

    No token, role or name goes into the redirect URL.
    """
    if not settings.GOOGLE_CLIENT_ID:
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE, "Google sign-in is not configured."
        )

    import httpx

    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            token_resp = await client.post(
                "https://oauth2.googleapis.com/token",
                data={
                    "code": code,
                    "client_id": settings.GOOGLE_CLIENT_ID,
                    "client_secret": settings.GOOGLE_CLIENT_SECRET,
                    "redirect_uri": settings.GOOGLE_REDIRECT_URI,
                    "grant_type": "authorization_code",
                },
            )
            tokens = token_resp.json()
            if "access_token" not in tokens:
                log.warning("google_token_exchange_failed")
                return RedirectResponse(
                    f"{settings.FRONTEND_URL}/login?error=google_failed"
                )

            profile_resp = await client.get(
                "https://www.googleapis.com/oauth2/v3/userinfo",
                headers={"Authorization": f"Bearer {tokens['access_token']}"},
            )
            profile = profile_resp.json()
    except httpx.HTTPError as exc:
        log.warning("google_oauth_http_error", error=str(exc))
        return RedirectResponse(f"{settings.FRONTEND_URL}/login?error=google_failed")

    email = (profile.get("email") or "").strip().lower()
    # Google's own verification flag, not the shape of the address.
    if not email or profile.get("email_verified") is not True:
        return RedirectResponse(
            f"{settings.FRONTEND_URL}/login?error=email_not_verified"
        )

    student = db.query(Student).filter(Student.email == email).first()
    created = False
    if student is None:
        student = Student(
            name=profile.get("name") or email.split("@")[0],
            email=email,
            role=Role.STUDENT.value,
            password_hash=None,
            password_algo="google_oauth",
            email_verified_at=utcnow(),
            token_version=0,
            is_active=True,
            enrolled_via="direct",
        )
        db.add(student)
        db.flush()
        token_service.get_or_create_wallet(db, "student", str(student.id))
        created = True
    elif student.email_verified_at is None:
        student.email_verified_at = utcnow()

    # Google's hosted-domain claim is trustworthy where an email suffix is not, so it can
    # satisfy the domain path — but only against a college whose domain an admin verified.
    hosted_domain = (profile.get("hd") or "").strip().lower()
    if hosted_domain and not student.college_id:
        enrollment.attempt_enrollment(db, student)

    handoff = verification.issue(
        db,
        purpose="oauth_handoff",
        subject=str(student.id),
        student_id=str(student.id),
        ttl_sec=settings.OAUTH_CODE_TTL_SEC,
        request_ip=ratelimit.client_ip(request),
        numeric=False,
    )
    audit.record(
        db,
        action="auth.google_login",
        actor_id=str(student.id),
        actor_role=normalize_role(student.role).value,
        detail={"created": created},
        ip=ratelimit.client_ip(request),
    )
    db.commit()

    return RedirectResponse(f"{settings.FRONTEND_URL}/auth/callback?code={handoff.code}")


@router.post("/google/exchange", response_model=TokenResponse)
async def google_exchange(
    body: OAuthExchangeRequest, request: Request, db: DbSession
) -> TokenResponse:
    """Swap the single-use hand-off code for an access token."""
    await ratelimit.limit_auth(request)
    try:
        row = verification.consume_token(
            db, purpose="oauth_handoff", code=body.code
        )
    except verification.VerificationError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from exc

    student = db.query(Student).filter(Student.id == row.student_id).first()
    if student is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Account not found")

    db.commit()
    db.refresh(student)
    return _issue(student, needs_verification=not student.is_phone_verified)


# ── Helpers ───────────────────────────────────────────────────────────────────


async def _send_email_otp(db: Session, student: Student, request: Request) -> None:
    """Best-effort verification email at signup.

    A delivery failure must not undo a successful registration, so this logs and returns;
    the user can request another code from the dashboard.
    """
    try:
        issued = verification.issue(
            db,
            purpose="email_otp",
            subject=(student.email or "").lower(),
            student_id=str(student.id),
            request_ip=ratelimit.client_ip(request),
        )
        db.commit()
        await notify.send_email_code(
            to=student.email or "", code=issued.code, purpose="email_otp"
        )
    except Exception as exc:
        log.warning("signup_otp_not_sent", error=str(exc)[:200])


def _verify_totp(secret: str | None, code: str) -> bool:
    if not secret:
        return False
    import pyotp

    # One step of drift either way, for clock skew on the user's phone.
    return bool(pyotp.TOTP(secret).verify(code, valid_window=1))


# ── TOTP enrolment ────────────────────────────────────────────────────────────
# PLAN.md §7.1 makes TOTP mandatory for admin accounts. Verification already existed
# in `login`; this is the enrolment half, which was missing — so the requirement was
# unenforceable in practice.


@router.post("/mfa/setup", response_model=MfaSetupResponse)
async def mfa_setup(principal: Admin, db: DbSession) -> MfaSetupResponse:
    """Generate a TOTP secret and the otpauth URI for an authenticator app.

    The secret is stored but `mfa_enabled` stays false until `/mfa/confirm` proves the
    authenticator works — otherwise a mistyped setup would lock the admin out of their
    own account on the next sign-in.

    Calling this again before confirming issues a fresh secret, which is what someone
    who abandoned a half-finished enrolment expects. Once enabled it refuses, so an
    attacker holding a live session cannot quietly swap the second factor.
    """
    import pyotp

    student = db.query(Student).filter(Student.id == principal.id).first()
    if student is None:  # pragma: no cover
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Not found")

    if getattr(student, "mfa_enabled", False):
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            "Two-factor authentication is already enabled on this account.",
        )

    secret = pyotp.random_base32()
    student.mfa_secret = secret
    db.commit()

    account = student.email or principal.id
    uri = pyotp.TOTP(secret).provisioning_uri(name=account, issuer_name="PlacePrep AI")
    log.info("mfa_setup_started", student_id=principal.id)
    return MfaSetupResponse(
        secret=secret,
        otpauth_uri=uri,
        issuer="PlacePrep AI",
        account=account,
        already_enabled=False,
    )


@router.post("/mfa/confirm", response_model=SimpleOk)
async def mfa_confirm(
    body: MfaConfirmRequest, request: Request, principal: Admin, db: DbSession
) -> SimpleOk:
    """Verify a code from the authenticator and turn TOTP on.

    Signs the account out everywhere on success: the next sign-in must present the
    second factor, so a session created before enrolment cannot outlive it.
    """
    await ratelimit.limit_by_user(principal.id, "mfa_confirm", "10/600")

    student = db.query(Student).filter(Student.id == principal.id).first()
    if student is None:  # pragma: no cover
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Not found")
    if not student.mfa_secret:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            "Start enrolment first — there is no pending secret for this account.",
        )
    if not _verify_totp(student.mfa_secret, body.code):
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            "That code is not correct. Check your authenticator's clock and try again.",
        )

    student.mfa_enabled = True
    student.token_version = int(getattr(student, "token_version", 0) or 0) + 1
    audit.record(
        db,
        action="auth.mfa_enabled",
        actor_id=principal.id,
        actor_role=principal.role.value,
        ip=ratelimit.client_ip(request),
    )
    db.commit()
    log.info("mfa_enabled", student_id=principal.id)
    return SimpleOk(
        ok=True,
        message="Two-factor authentication is on. Sign in again with your "
        "authenticator code.",
    )

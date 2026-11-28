"""One-time codes: OTPs, magic links, OAuth hand-off, admin invites.

Codes are stored as a keyed hash, never in the clear, so a database dump does not hand
over live codes. Each code is single use, expires, and counts failed attempts so it cannot
be brute-forced — a 6-digit OTP with unlimited attempts is a 6-digit lock with no door.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta
from typing import Any, Literal

from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.logging_setup import get_logger
from app.core.security import hash_lookup_key, new_numeric_otp, new_one_time_code
from app.models import VerificationCode, utcnow

log = get_logger("verification")

Purpose = Literal[
    "email_otp", "phone_otp", "magic_link", "oauth_handoff", "admin_invite"
]


class VerificationError(RuntimeError):
    """The code was wrong, expired, already used, or tried too many times."""


@dataclass(frozen=True)
class IssuedCode:
    #: The plaintext code. Send it, then forget it — it is not stored anywhere.
    code: str
    expires_in_sec: int


def issue(
    db: Session,
    *,
    purpose: Purpose,
    subject: str,
    student_id: str | None = None,
    payload: dict[str, Any] | None = None,
    ttl_sec: int | None = None,
    request_ip: str | None = None,
    numeric: bool = True,
) -> IssuedCode:
    """Create a code for `subject`, invalidating any earlier unused code for it.

    Caller commits. Invalidating the previous code matters: otherwise requesting a new
    OTP leaves several valid at once, multiplying an attacker's chances.
    """
    now = utcnow()
    ttl = ttl_sec or settings.OTP_TTL_SEC

    db.query(VerificationCode).filter(
        VerificationCode.purpose == purpose,
        VerificationCode.subject == subject,
        VerificationCode.consumed_at.is_(None),
    ).update({"consumed_at": now}, synchronize_session=False)

    code = new_numeric_otp() if numeric else new_one_time_code()
    db.add(
        VerificationCode(
            purpose=purpose,
            subject=subject,
            code_hash=hash_lookup_key(code),
            student_id=student_id,
            payload=payload,
            max_attempts=settings.OTP_MAX_ATTEMPTS,
            expires_at=now + timedelta(seconds=ttl),
            request_ip=request_ip,
        )
    )
    log.info("verification_code_issued", purpose=purpose, subject=_mask(subject))
    return IssuedCode(code=code, expires_in_sec=ttl)


def consume(
    db: Session, *, purpose: Purpose, subject: str, code: str
) -> VerificationCode:
    """Validate and burn a code. Raises `VerificationError` on any failure.

    Caller commits.
    """
    now = utcnow()
    row = (
        db.query(VerificationCode)
        .filter(
            VerificationCode.purpose == purpose,
            VerificationCode.subject == subject,
            VerificationCode.consumed_at.is_(None),
        )
        .order_by(VerificationCode.created_at.desc())
        .first()
    )
    if row is None:
        raise VerificationError("No active code. Request a new one.")

    if row.expires_at <= now:
        row.consumed_at = now
        raise VerificationError("That code has expired. Request a new one.")

    if int(row.attempts) >= int(row.max_attempts):
        row.consumed_at = now
        raise VerificationError("Too many incorrect attempts. Request a new code.")

    if row.code_hash != hash_lookup_key(code.strip()):
        row.attempts = int(row.attempts) + 1
        # Commit the attempt counter even though verification failed, or the counter
        # would roll back with the request and the limit would never bite.
        db.commit()
        raise VerificationError("That code is not correct.")

    row.consumed_at = now
    log.info("verification_code_consumed", purpose=purpose, subject=_mask(subject))
    return row


def consume_token(db: Session, *, purpose: Purpose, code: str) -> VerificationCode:
    """Validate a long random code where the subject is not known up front.

    Used for the OAuth hand-off and admin invites, where the client presents only the
    code. Looks the row up by hash, so the code itself is still never stored.
    """
    now = utcnow()
    row = (
        db.query(VerificationCode)
        .filter(
            VerificationCode.purpose == purpose,
            VerificationCode.code_hash == hash_lookup_key(code.strip()),
            VerificationCode.consumed_at.is_(None),
        )
        .first()
    )
    if row is None:
        raise VerificationError("That link is not valid.")
    if row.expires_at <= now:
        row.consumed_at = now
        raise VerificationError("That link has expired.")

    row.consumed_at = now
    return row


def _mask(subject: str) -> str:
    """Partially mask an email or phone number for logs."""
    if "@" in subject:
        local, _, domain = subject.partition("@")
        return f"{local[:2]}***@{domain}"
    return f"***{subject[-3:]}" if len(subject) > 3 else "***"

"""Password hashing and JWT issuing/validation.

Two things worth knowing:

* Passwords were previously stored as unsalted SHA-256. We cannot un-hash them, so
  `verify_password` accepts the legacy digest and reports that the caller should re-hash
  with bcrypt. `api/auth.py` does that on successful login, and `students.password_algo`
  tracks which rows are still legacy.
* The JWT deliberately carries almost nothing: subject plus a token version. Role and
  college are read from the database on every request (PLAN.md §7.2), so a stolen or
  edited token cannot escalate privileges, and bumping `students.token_version`
  invalidates every outstanding session for that user.
"""

from __future__ import annotations

import hashlib
import hmac
import secrets
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import bcrypt
import jwt as pyjwt

from app.core.config import settings

_BCRYPT_PREFIXES = ("$2a$", "$2b$", "$2y$")


# ── Passwords ─────────────────────────────────────────────────────────────────


def hash_password(password: str) -> str:
    """Hash a password with bcrypt. Returns a self-describing hash string."""
    _validate_password_strength(password)
    salt = bcrypt.gensalt(rounds=settings.BCRYPT_ROUNDS)
    return bcrypt.hashpw(_prehash(password), salt).decode("utf-8")


@dataclass(frozen=True)
class PasswordCheck:
    ok: bool
    #: True when the stored hash used the legacy algorithm and should be upgraded.
    needs_rehash: bool


def verify_password(password: str, stored_hash: str | None) -> PasswordCheck:
    """Check a password against either a bcrypt hash or a legacy SHA-256 digest."""
    if not stored_hash:
        return PasswordCheck(ok=False, needs_rehash=False)

    if stored_hash.startswith(_BCRYPT_PREFIXES):
        try:
            ok = bcrypt.checkpw(_prehash(password), stored_hash.encode("utf-8"))
        except ValueError:
            return PasswordCheck(ok=False, needs_rehash=False)
        return PasswordCheck(ok=ok, needs_rehash=False)

    # Accounts created through Google OAuth carry this sentinel and have no password.
    if stored_hash == "google_oauth":
        return PasswordCheck(ok=False, needs_rehash=False)

    # Legacy: unsalted SHA-256 hex digest.
    legacy = hashlib.sha256(password.encode("utf-8")).hexdigest()
    ok = hmac.compare_digest(legacy, stored_hash)
    return PasswordCheck(ok=ok, needs_rehash=ok)


def _prehash(password: str) -> bytes:
    """Work around bcrypt's 72-byte input limit without truncating silently.

    SHA-256 first, then base-16 encode, so every password maps to a fixed 64-byte input.
    """
    return hashlib.sha256(password.encode("utf-8")).hexdigest().encode("ascii")


def _validate_password_strength(password: str) -> None:
    if len(password) < 8:
        raise ValueError("Password must be at least 8 characters")
    if len(password) > 256:
        raise ValueError("Password must be at most 256 characters")


# ── JWTs ──────────────────────────────────────────────────────────────────────


class TokenError(Exception):
    """Raised when a token is missing, malformed, expired or stale."""


def make_access_token(user_id: str, token_version: int) -> str:
    now = datetime.now(UTC)
    payload = {
        "sub": str(user_id),
        "tv": int(token_version),
        "iat": int(now.timestamp()),
        "exp": int((now + timedelta(hours=settings.JWT_EXPIRE_HOURS)).timestamp()),
        "jti": secrets.token_urlsafe(8),
    }
    return pyjwt.encode(payload, settings.JWT_SECRET, algorithm=settings.JWT_ALGORITHM)


@dataclass(frozen=True)
class TokenClaims:
    user_id: str
    token_version: int


def decode_access_token(token: str) -> TokenClaims:
    try:
        payload = pyjwt.decode(
            token,
            settings.JWT_SECRET,
            algorithms=[settings.JWT_ALGORITHM],
            options={"require": ["exp", "sub"]},
        )
    except pyjwt.ExpiredSignatureError as exc:
        raise TokenError("Token expired") from exc
    except pyjwt.PyJWTError as exc:
        raise TokenError("Invalid token") from exc

    sub = payload.get("sub")
    if not sub:
        raise TokenError("Token missing subject")
    return TokenClaims(user_id=str(sub), token_version=int(payload.get("tv", 0)))


# ── One-time codes (OAuth hand-off, OTP) ──────────────────────────────────────


def new_one_time_code(nbytes: int = 32) -> str:
    """URL-safe single-use code. Used instead of putting a JWT in a redirect URL."""
    return secrets.token_urlsafe(nbytes)


def new_numeric_otp(digits: int = 6) -> str:
    upper = 10**digits
    return str(secrets.randbelow(upper)).zfill(digits)


def hash_lookup_key(value: str) -> str:
    """Deterministic hash for storing OTP/code keys without storing the secret itself."""
    return hashlib.sha256(
        (settings.JWT_SECRET + ":" + value).encode("utf-8")
    ).hexdigest()


def constant_time_equals(a: str, b: str) -> bool:
    return hmac.compare_digest(a.encode("utf-8"), b.encode("utf-8"))

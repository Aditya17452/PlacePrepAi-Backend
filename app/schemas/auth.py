"""Registration, login, verification and OAuth payloads."""

from __future__ import annotations

from typing import Literal

from pydantic import EmailStr, Field, field_validator

from app.schemas.common import OutModel, RoleName, StrictModel


class RegisterRequest(StrictModel):
    """Note the absence of a `role` field — roles are assigned by the server only."""

    name: str = Field(min_length=1, max_length=200)
    email: EmailStr
    password: str = Field(min_length=8, max_length=256)
    phone: str | None = Field(default=None, max_length=20)
    cgpa: float | None = Field(default=None, ge=0, le=10)
    branch: str | None = Field(default=None, max_length=100)
    year_of_passing: int | None = Field(default=None, ge=1990, le=2100)
    #: Optional college join code. Never sufficient on its own (PLAN.md §7.3).
    join_code: str | None = Field(default=None, max_length=32)

    @field_validator("phone")
    @classmethod
    def _normalise_phone(cls, v: str | None) -> str | None:
        if v is None:
            return None
        digits = "".join(ch for ch in v if ch.isdigit() or ch == "+")
        if len(digits.lstrip("+")) < 8:
            raise ValueError("Phone number looks too short")
        return digits


class LoginRequest(StrictModel):
    email: EmailStr
    password: str = Field(min_length=1, max_length=256)
    #: Required once the account has TOTP enabled (mandatory for admins).
    mfa_code: str | None = Field(default=None, min_length=6, max_length=6)


class TokenResponse(OutModel):
    token: str
    role: RoleName
    name: str
    id: str
    #: True when the account still needs email or phone verification to do anything.
    needs_verification: bool = False


class MeResponse(OutModel):
    id: str
    name: str
    email: str
    role: RoleName
    branch: str | None = None
    cgpa: float | None = None
    college_id: str | None = None
    college_name: str | None = None
    email_verified: bool = False
    phone_verified: bool = False
    mfa_enabled: bool = False
    #: Spendable tokens: personal wallet plus the remaining college allowance.
    tokens_available: int = 0
    personal_tokens: int = 0
    college_allowance_remaining: int = 0


class OAuthUrlResponse(OutModel):
    auth_url: str


class OAuthExchangeRequest(StrictModel):
    """Exchanges the one-time code from the OAuth redirect for a real token.

    The callback no longer puts a JWT in the redirect URL, where it would land in browser
    history, the Referer header and proxy logs.
    """

    code: str = Field(min_length=16, max_length=256)


class SendOtpRequest(StrictModel):
    channel: Literal["email", "phone"]
    #: Omitted means "the address already on my account".
    destination: str | None = Field(default=None, max_length=200)


class VerifyOtpRequest(StrictModel):
    channel: Literal["email", "phone"]
    code: str = Field(min_length=4, max_length=8)

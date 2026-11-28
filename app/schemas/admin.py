"""Platform-admin payloads: enrolment, invites, stats, audit."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import EmailStr, Field

from app.schemas.common import OutModel, StrictModel


class EnrollCollegeRequest(StrictModel):
    """Creates the college and *invites* its admin.

    No password field: we never set a password on someone else's behalf. The invited
    college admin receives a one-time link and chooses their own.
    """

    name: str = Field(min_length=1, max_length=200)
    admin_name: str = Field(min_length=1, max_length=200)
    admin_email: EmailStr
    #: Optional, and only meaningful once ownership is verified.
    domain: str | None = Field(default=None, max_length=100)
    monthly_allowance_default: int = Field(default=4, ge=0, le=1_000)
    daily_interview_cap: int = Field(default=3, ge=1, le=50)


class AcceptInviteRequest(StrictModel):
    code: str = Field(min_length=16, max_length=256)
    password: str = Field(min_length=8, max_length=256)
    mfa_code: str | None = Field(default=None, min_length=6, max_length=6)


class PlatformStatsResponse(OutModel):
    total_students: int
    total_colleges: int
    total_interviews: int
    completed_interviews: int
    tokens_outstanding: int
    tokens_consumed: int
    revenue_paise: int
    #: The §9 guardrail metric.
    cost_per_interview_inr: float | None = None
    llm_cost_usd_30d: float = 0.0


class AuditLogOut(OutModel):
    id: str
    actor_id: str | None
    actor_role: str | None
    action: str
    target: str | None
    detail: dict[str, Any] | None
    created_at: datetime

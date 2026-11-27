"""College-admin payloads: roster, student summaries, pool."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import EmailStr, Field

from app.schemas.common import OutModel, StrictModel


class RosterRowIn(StrictModel):
    enrollment_no: str | None = Field(default=None, max_length=60)
    name: str | None = Field(default=None, max_length=200)
    batch: str | None = Field(default=None, max_length=40)
    branch: str | None = Field(default=None, max_length=100)
    email: EmailStr | None = None
    phone: str | None = Field(default=None, max_length=20)
    valid_until: datetime | None = None


class RosterUploadRequest(StrictModel):
    rows: list[RosterRowIn] = Field(min_length=1, max_length=5_000)
    #: Deactivate roster rows absent from this upload.
    replace_existing: bool = False


class RosterUploadResponse(OutModel):
    created: int
    updated: int
    deactivated: int
    skipped: list[dict[str, Any]] = Field(default_factory=list)


class StudentSummaryOut(OutModel):
    id: str
    name: str
    email: str
    branch: str | None = None
    cgpa: float | None = None
    interviews_taken: int = 0
    latest_score: float | None = None
    latest_integrity: float | None = None
    allowance_remaining: int | None = None


class CollegeOut(OutModel):
    id: str
    name: str
    domain: str | None = None
    is_active: bool = True
    pool_balance: int = 0
    pool_granted: int = 0
    students: int = 0
    monthly_allowance_default: int = 4
    daily_interview_cap: int = 3
    distribution_frozen: bool = False

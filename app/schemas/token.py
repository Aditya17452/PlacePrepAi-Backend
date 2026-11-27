"""Wallet, ledger, grants and college allowance payloads."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import Field

from app.schemas.common import OutModel, StrictModel


class WalletResponse(OutModel):
    personal_balance: int
    personal_reserved: int
    college_allowance_remaining: int | None = None
    college_pool_remaining: int | None = None
    total_available: int
    next_expiry: datetime | None = None
    next_expiry_qty: int | None = None


class LedgerEntryOut(OutModel):
    id: str
    kind: str
    amount: int
    reason: str
    session_id: str | None = None
    created_at: datetime


class GrantTokensRequest(StrictModel):
    """Admin-only. A reason is mandatory — it lands in the audit log and the ledger."""

    college_id: str = Field(max_length=36)
    tokens: int = Field(gt=0, le=100_000)
    reason: str = Field(min_length=3, max_length=500)
    validity_days: int | None = Field(default=None, gt=0, le=1_095)


class RevokeTokensRequest(StrictModel):
    college_id: str = Field(max_length=36)
    tokens: int = Field(gt=0, le=100_000)
    reason: str = Field(min_length=3, max_length=500)


class AllowanceGroupRequest(StrictModel):
    name: str = Field(min_length=1, max_length=200)
    per_student_allowance: int = Field(ge=0, le=1_000)
    period: Literal["month", "term"] = "month"
    #: Roster filters, e.g. {"batch": "2027", "branch": "CSE"}.
    filters: dict[str, Any] = Field(default_factory=dict)
    valid_from: datetime | None = None
    valid_until: datetime | None = None


class AllowanceOverrideRequest(StrictModel):
    student_id: str = Field(max_length=36)
    kind: Literal["topup", "drive"] = "topup"
    extra_allowance: int = Field(ge=0, le=1_000)
    reason: str = Field(min_length=3, max_length=500)
    label: str | None = Field(default=None, max_length=200)
    jd_id: str | None = Field(default=None, max_length=36)
    valid_from: datetime | None = None
    valid_until: datetime | None = None

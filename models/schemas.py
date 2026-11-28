"""Pydantic request and response models.

This file was empty; most endpoints took `body: dict` and returned ad-hoc dictionaries,
so nothing was validated on the way in and nothing was documented on the way out.

Two conventions worth noting:

* Request models use `extra="forbid"`. An unexpected field is a 422, not something
  silently ignored — that is what let `{"role": "officer"}` through the register endpoint.
* No request model accepts an identity (`student_id`) or a price. Identity comes from the
  bearer token, prices come from the `plans` table.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, EmailStr, Field, field_validator

# ── Base classes ──────────────────────────────────────────────────────────────


class StrictModel(BaseModel):
    """Request bodies: reject unknown fields rather than ignoring them."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class OutModel(BaseModel):
    """Responses: allow ORM objects to be returned directly."""

    model_config = ConfigDict(from_attributes=True)


RoleName = Literal["student", "college_admin", "admin"]
Phase = Literal["warmup", "technical", "closing", "complete"]


# ── Auth ──────────────────────────────────────────────────────────────────────


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


class SimpleOk(OutModel):
    ok: bool = True
    message: str | None = None


# ── Resume & candidate context ────────────────────────────────────────────────


class JDMatchOut(OutModel):
    company: str
    role: str
    matchScore: int = Field(ge=0, le=100)
    status: str
    matchedSkills: list[str] = Field(default_factory=list)
    missingSkills: list[str] = Field(default_factory=list)
    reasoning: str = ""
    jdId: str


class ResumeUploadResponse(OutModel):
    status: Literal["ready", "error", "processing"]
    suggestions: list[JDMatchOut] = Field(default_factory=list)
    message: str | None = None
    #: Where the stored resume lives, for the review-and-correct screen.
    profile_version: int | None = None


class CandidateLinkIn(StrictModel):
    kind: Literal["github", "leetcode", "linkedin", "portfolio"]
    url: str = Field(max_length=500)


class ConsentRequest(StrictModel):
    kind: Literal["proctoring", "profile_processing", "college_sharing"]
    version: str = Field(max_length=20)
    granted: bool = True


# ── Job descriptions ──────────────────────────────────────────────────────────


class SubmitJDRequest(StrictModel):
    company_name: str = Field(min_length=1, max_length=200)
    role_title: str = Field(min_length=1, max_length=200)
    jd_text: str = Field(min_length=50, max_length=20_000)


class StudentJDOut(OutModel):
    id: str
    company_name: str
    role_title: str
    match_pct: int = 0
    matched_skills: list[str] = Field(default_factory=list)
    missing_skills: list[str] = Field(default_factory=list)
    advice: str = ""
    status: str
    created_at: datetime | None = None


class JDListItem(OutModel):
    id: str
    company: str
    role: str


# ── Interviews ────────────────────────────────────────────────────────────────


class StartInterviewRequest(StrictModel):
    """Exactly one of `jd_id` or `custom_jd_id`. No `student_id` — that comes from the
    bearer token, so an interview can no longer be attributed to another student."""

    jd_id: str | None = Field(default=None, max_length=36)
    custom_jd_id: str | None = Field(default=None, max_length=36)
    #: Set false when the webcam is unavailable; the session runs but is marked unproctored.
    proctoring_enabled: bool = True

    @field_validator("custom_jd_id")
    @classmethod
    def _exactly_one(cls, v: str | None, info: Any) -> str | None:
        jd_id = info.data.get("jd_id")
        if bool(jd_id) == bool(v):
            raise ValueError("Provide exactly one of jd_id or custom_jd_id")
        return v


class StartInterviewResponse(OutModel):
    sessionId: str
    openingQuestion: str
    company: str
    role: str
    maxQuestions: int
    maxMinutes: int
    #: Tokens left after the reservation, so the UI can show it without another call.
    tokens_remaining: int = 0


class SubmitTurnRequest(StrictModel):
    """One answer. Replaces the WebSocket `turn_complete` message (PLAN.md §0.1).

    `turn_id` is generated by the client and makes submission idempotent: a retry after a
    dropped connection returns the original next question instead of burning a turn.
    """

    turn_id: str = Field(min_length=8, max_length=64)
    transcript: str = Field(default="", max_length=20_000)
    #: Base64 audio, used only when browser STT produced nothing.
    audio: str | None = Field(default=None, max_length=8_000_000)
    answered_question: str | None = Field(default=None, max_length=4_000)
    client_debug: dict[str, Any] | None = None


class FeedbackOut(OutModel):
    tip: str
    tone: Literal["positive", "neutral", "needs_work"]


class SubmitTurnResponse(OutModel):
    question: str | None
    phase: Phase
    questions_asked: int
    questions_remaining: int
    session_complete: bool
    feedback: FeedbackOut | None = None
    transcript_used: str = ""
    #: True when this turn crossed the threshold that consumes the reserved token.
    token_consumed: bool = False


class VisionScores(StrictModel):
    posture_score: int = Field(ge=0, le=100)
    eye_contact_score: int = Field(ge=0, le=100)


class ProctoringEventIn(StrictModel):
    type: str = Field(max_length=40)
    severity: int = Field(ge=1, le=5)
    client_ts: datetime
    detail: dict[str, Any] | None = None
    #: Base64 low-res frame, only sent for violations.
    snapshot: str | None = Field(default=None, max_length=2_000_000)


class ProctoringBatchRequest(StrictModel):
    """Events are batched over HTTP every few seconds instead of streamed."""

    events: list[ProctoringEventIn] = Field(max_length=50)
    vision: VisionScores | None = None


class ProctoringBatchResponse(OutModel):
    accepted: int
    #: The server decides termination, not the client (PLAN.md §4 Phase 4).
    terminate: bool = False
    terminate_reason: str | None = None
    warnings_issued: int = 0


class EndInterviewRequest(StrictModel):
    reason: Literal["student_ended", "completed", "client_error"] = "student_ended"


class QuestionBreakdownOut(OutModel):
    turn: int
    question: str
    answer: str
    phase: str | None = None
    competency: str | None = None
    difficulty: int | None = None
    weight: float | None = None
    score: float | None = None
    #: Phase 3: why the engine chose this question.
    rationale: str | None = None


class ReportCategories(OutModel):
    technicalAccuracy: float | None = None
    communication: float | None = None
    problemSolving: float | None = None
    confidence: float | None = None
    bodyLanguage: float | None = None


class ReportResponse(OutModel):
    status: Literal["processing", "complete", "failed"]
    overallScore: float | None = None
    verdict: str | None = None
    categories: ReportCategories | None = None
    strengths: list[str] = Field(default_factory=list)
    improvements: list[str] = Field(default_factory=list)
    tips: list[str] = Field(default_factory=list)
    topics: list[dict[str, Any]] = Field(default_factory=list)
    questionBreakdown: list[QuestionBreakdownOut] = Field(default_factory=list)
    summary: str | None = None
    #: Shown next to the technical score, never folded into it.
    integrityScore: float | None = None
    integrityTimeline: list[dict[str, Any]] = Field(default_factory=list)
    proctored: bool = True
    scoringVersion: int = 1


class SessionHistoryItem(OutModel):
    id: str
    company: str
    role: str
    date: str
    score: float | None = None
    status: str


# ── Tokens ────────────────────────────────────────────────────────────────────


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


# ── Payments ──────────────────────────────────────────────────────────────────


class PlanOut(OutModel):
    code: str
    name: str
    price_paise: int
    tokens: int
    validity_days: int | None
    is_recommended: bool = False
    requires_phone_verification: bool = False


class CreateOrderRequest(StrictModel):
    """Only a plan code. The server looks up price and token count."""

    plan_code: str = Field(min_length=1, max_length=40)


class CreateOrderResponse(OutModel):
    order_id: str
    amount_paise: int
    currency: str
    razorpay_key_id: str
    plan: PlanOut


class PaymentStatusResponse(OutModel):
    """What the client-side callback gets: status only.

    Tokens are granted by the webhook, so this endpoint cannot be replayed for credit.
    """

    order_id: str
    status: str
    tokens_granted: int
    message: str


class RefundRequest(StrictModel):
    payment_id: str = Field(max_length=64)
    reason: str = Field(min_length=3, max_length=500)
    #: Omitted means a full refund.
    amount_paise: int | None = Field(default=None, gt=0)


# ── College admin ─────────────────────────────────────────────────────────────


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


# ── Admin ─────────────────────────────────────────────────────────────────────


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


# ── Health ────────────────────────────────────────────────────────────────────


class HealthResponse(OutModel):
    status: Literal["ok"]
    env: str
    version: str


class ReadyResponse(OutModel):
    status: Literal["ready", "degraded"]
    database: bool
    redis: bool
    detail: dict[str, str] = Field(default_factory=dict)

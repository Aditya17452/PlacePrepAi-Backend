"""Pydantic request and response models, one module per domain.

Re-exported here so routers write `from app.schemas import LoginRequest` and are
unaffected when a model moves between domain modules.

No request model accepts an identity (`student_id`) or a price: identity comes from the
bearer token, prices come from the `plans` table.
"""

from __future__ import annotations

from app.schemas.admin import (
    AcceptInviteRequest,
    AuditLogOut,
    EnrollCollegeRequest,
    PlatformStatsResponse,
)
from app.schemas.auth import (
    LoginRequest,
    MeResponse,
    MfaConfirmRequest,
    MfaSetupResponse,
    OAuthExchangeRequest,
    OAuthUrlResponse,
    RegisterRequest,
    SendOtpRequest,
    TokenResponse,
    VerifyOtpRequest,
)
from app.schemas.candidate import (
    CandidateLinkIn,
    ConsentRequest,
    ConsentStatusOut,
    JDMatchOut,
    ResumeUploadResponse,
)
from app.schemas.college import (
    CollegeOut,
    RosterRowIn,
    RosterUploadRequest,
    RosterUploadResponse,
    StudentSummaryOut,
)
from app.schemas.common import (
    HealthResponse,
    OutModel,
    Phase,
    ReadyResponse,
    RoleName,
    SimpleOk,
    StrictModel,
)
from app.schemas.interview import (
    EndInterviewRequest,
    FeedbackOut,
    QuestionBreakdownOut,
    ReportCategories,
    ReportResponse,
    SessionHistoryItem,
    StartInterviewRequest,
    StartInterviewResponse,
    SubmitTurnRequest,
    SubmitTurnResponse,
)
from app.schemas.jd import JDListItem, StudentJDOut, SubmitJDRequest
from app.schemas.payment import (
    CreateOrderRequest,
    CreateOrderResponse,
    PaymentStatusResponse,
    PlanOut,
    RefundRequest,
)
from app.schemas.proctoring import (
    ProctoringBatchRequest,
    ProctoringBatchResponse,
    ProctoringEventIn,
    VisionScores,
)
from app.schemas.token import (
    AllowanceGroupRequest,
    AllowanceOverrideRequest,
    GrantTokensRequest,
    LedgerEntryOut,
    RevokeTokensRequest,
    WalletResponse,
)

__all__ = [
    # Common
    "StrictModel",
    "OutModel",
    "RoleName",
    "Phase",
    "SimpleOk",
    "HealthResponse",
    "ReadyResponse",
    # Auth
    "RegisterRequest",
    "LoginRequest",
    "TokenResponse",
    "MeResponse",
    "MfaSetupResponse",
    "MfaConfirmRequest",
    "OAuthUrlResponse",
    "OAuthExchangeRequest",
    "SendOtpRequest",
    "VerifyOtpRequest",
    # Candidate
    "JDMatchOut",
    "ResumeUploadResponse",
    "CandidateLinkIn",
    "ConsentRequest",
    "ConsentStatusOut",
    # Job descriptions
    "SubmitJDRequest",
    "StudentJDOut",
    "JDListItem",
    # Interview
    "StartInterviewRequest",
    "StartInterviewResponse",
    "SubmitTurnRequest",
    "SubmitTurnResponse",
    "FeedbackOut",
    "EndInterviewRequest",
    "QuestionBreakdownOut",
    "ReportCategories",
    "ReportResponse",
    "SessionHistoryItem",
    # Proctoring
    "VisionScores",
    "ProctoringEventIn",
    "ProctoringBatchRequest",
    "ProctoringBatchResponse",
    # Tokens
    "WalletResponse",
    "LedgerEntryOut",
    "GrantTokensRequest",
    "RevokeTokensRequest",
    "AllowanceGroupRequest",
    "AllowanceOverrideRequest",
    # Payments
    "PlanOut",
    "CreateOrderRequest",
    "CreateOrderResponse",
    "PaymentStatusResponse",
    "RefundRequest",
    # College admin
    "RosterRowIn",
    "RosterUploadRequest",
    "RosterUploadResponse",
    "StudentSummaryOut",
    "CollegeOut",
    # Admin
    "EnrollCollegeRequest",
    "AcceptInviteRequest",
    "PlatformStatsResponse",
    "AuditLogOut",
]

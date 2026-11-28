"""SQLAlchemy models, one module per domain.

Everything is re-exported here so callers write `from app.models import Student` and stay
unaffected when a class moves between domain modules. Importing this package also
registers every table on `Base.metadata`, which is what Alembic autogenerate and the test
fixtures depend on.
"""

from __future__ import annotations

from app.models.base import Base, SessionLocal, engine, gen_uuid, utcnow
from app.models.billing import (
    IdempotencyKey,
    JobQueue,
    LlmUsage,
    Payment,
    Plan,
)
from app.models.college import College
from app.models.identity import (
    ApiAuditLog,
    Consent,
    EnrollmentRequest,
    JoinCode,
    RosterEntry,
    VerificationCode,
)
from app.models.interview import (
    InterviewSession,
    SessionScore,
    SessionTranscript,
    Shortlist,
    StudentProfileData,
)
from app.models.jd import JDChunk, JobDescription, StudentJD
from app.models.proctoring import ProctoringEvent, ProctoringSummary
from app.models.token import (
    AllowanceGroup,
    AllowanceMember,
    AllowanceOverride,
    TokenLedger,
    TokenLot,
    TokenWallet,
)
from app.models.user import Student

__all__ = [
    # Infrastructure
    "Base",
    "SessionLocal",
    "engine",
    "gen_uuid",
    "utcnow",
    # People
    "Student",
    "College",
    # Job descriptions
    "JobDescription",
    "JDChunk",
    "StudentJD",
    # Interviews
    "InterviewSession",
    "SessionTranscript",
    "SessionScore",
    "Shortlist",
    "StudentProfileData",
    # Tokens
    "TokenWallet",
    "TokenLot",
    "TokenLedger",
    "AllowanceGroup",
    "AllowanceMember",
    "AllowanceOverride",
    # Billing
    "Plan",
    "Payment",
    "IdempotencyKey",
    "LlmUsage",
    "JobQueue",
    # Identity
    "VerificationCode",
    "RosterEntry",
    "JoinCode",
    "EnrollmentRequest",
    "Consent",
    "ApiAuditLog",
    # Proctoring
    "ProctoringEvent",
    "ProctoringSummary",
]

"""Client-reported proctoring observations and the server's decision."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import Field

from app.schemas.common import OutModel, StrictModel


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

"""Resume upload, extracted profile and consent payloads."""

from __future__ import annotations

from typing import Literal

from pydantic import Field

from app.schemas.common import OutModel, StrictModel


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

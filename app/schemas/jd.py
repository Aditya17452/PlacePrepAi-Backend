"""Job-description payloads, both seeded roles and student-pasted ones."""

from __future__ import annotations

from datetime import datetime

from pydantic import Field

from app.schemas.common import OutModel, StrictModel


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

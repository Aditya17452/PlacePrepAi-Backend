"""Shared base classes, aliases and trivial responses.

Two conventions the rest of the schema modules rely on:

* Request bodies subclass `StrictModel`, which forbids unknown fields. An unexpected
  field is a 422 rather than something silently ignored — that is what let
  `{"role": "officer"}` through the old register endpoint.
* Responses subclass `OutModel`, which reads attributes off ORM objects.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class StrictModel(BaseModel):
    """Request bodies: reject unknown fields rather than ignoring them."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class OutModel(BaseModel):
    """Responses: allow ORM objects to be returned directly."""

    model_config = ConfigDict(from_attributes=True)


RoleName = Literal["student", "college_admin", "admin"]
Phase = Literal["warmup", "technical", "closing", "complete"]


class SimpleOk(OutModel):
    ok: bool = True
    message: str | None = None


class HealthResponse(OutModel):
    status: Literal["ok"]
    env: str
    version: str


class ReadyResponse(OutModel):
    status: Literal["ready", "degraded"]
    database: bool
    redis: bool
    detail: dict[str, str] = Field(default_factory=dict)

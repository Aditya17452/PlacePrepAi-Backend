"""Database access helpers.

Deliberately thin for now. The structural move in this change relocated modules without
altering behaviour, and extracting every inline query out of the routers and services into
repository functions is a refactor, not a move — it is listed as follow-up work in the PR
rather than smuggled in here.

`session` re-exports the session factory so callers do not reach into `app.models.base`
for it.
"""

from __future__ import annotations

from app.models.base import SessionLocal, engine

__all__ = ["SessionLocal", "engine"]

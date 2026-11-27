"""Idempotent seeding. Run explicitly: `python -m scripts.seed`.

This used to happen on every application startup (`create_tables()` called `seed_jds()`),
which meant the app wrote data as a side effect of booting. Seeding is now a deliberate
command, safe to re-run.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.models import (  # noqa: E402
    JobDescription,
    Plan,  # noqa: E402
    SessionLocal,
)
from app.seeds.jd_templates import JD_TEMPLATES  # noqa: E402
from app.seeds.plans import ALL_PLANS  # noqa: E402
from app.services.rag_service import index_jd  # noqa: E402


def seed_plans(db) -> int:  # type: ignore[no-untyped-def]
    """Insert missing plans. Existing rows are left alone — prices may have been
    changed deliberately in the database."""
    added = 0
    for spec in ALL_PLANS:
        if db.query(Plan).filter(Plan.code == spec["code"]).first() is not None:
            continue
        db.add(Plan(**spec))
        added += 1
    db.commit()
    return added


def seed_job_descriptions(db) -> tuple[int, int]:  # type: ignore[no-untyped-def]
    """Insert the role templates and index them for retrieval.

    Indexing matters: `jd_chunks` was empty in production, so every "JD-aware" question
    was generated from the role title alone.
    """
    added = 0
    indexed = 0
    for spec in JD_TEMPLATES:
        existing = (
            db.query(JobDescription)
            .filter(
                JobDescription.role_title == spec["role_title"],
                JobDescription.company_name == spec["company_name"],
            )
            .first()
        )
        if existing is None:
            existing = JobDescription(
                company_name=spec["company_name"],
                role_title=spec["role_title"],
                required_skills=spec["required_skills"],
                jd_text=spec["jd_text"],
                is_active=True,
            )
            db.add(existing)
            db.flush()
            added += 1
        indexed += index_jd(db, str(existing.id), existing.jd_text or "")
    db.commit()
    return added, indexed


def backfill_jd_chunks(db) -> int:  # type: ignore[no-untyped-def]
    """Index every JD that has no chunks yet, including ones created before Phase 1."""
    from app.models import JDChunk

    total = 0
    for jd in db.query(JobDescription).all():
        has_chunks = (
            db.query(JDChunk).filter(JDChunk.jd_id == str(jd.id)).first() is not None
        )
        if not has_chunks and jd.jd_text:
            total += index_jd(db, str(jd.id), jd.jd_text)
    db.commit()
    return total


def main() -> None:
    db = SessionLocal()
    try:
        plans = seed_plans(db)
        jds, indexed = seed_job_descriptions(db)
        backfilled = backfill_jd_chunks(db)
        print(f"plans added:        {plans}")
        print(f"job descriptions:   {jds} added, {indexed} chunks indexed")
        print(f"backfilled chunks:  {backfilled}")
    finally:
        db.close()


if __name__ == "__main__":
    main()

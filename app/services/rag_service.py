"""Retrieving the parts of a job description relevant to what the candidate just said.

Two things were wrong before:

* `get_embedder()` imported `sentence-transformers` — a ~2 GB torch install — and had no
  call sites anywhere. Retrieval has always been keyword-based. The dependency is gone.
* `jd_chunks` was **empty in production** (0 rows), so `retrieve_jd_context` returned
  "No specific JD requirements found." on every single turn. Every "JD-aware" question
  was really generated from the role title alone. `index_jd` is now called whenever a JD
  is created or edited, and a backfill migration indexes the existing rows.

Still keyword search, deliberately: it is free, it works for the volumes involved, and
PLAN.md §0 defers pgvector to Phase 3 where it can be judged against the engine.
"""

from __future__ import annotations

import asyncio
import re

from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.logging_setup import get_logger
from app.models import JDChunk, SessionLocal

log = get_logger("rag")

CHUNK_WORDS = 150

#: Words too common to discriminate between chunks.
_STOPWORDS = frozenset(
    {
        "about",
        "above",
        "after",
        "again",
        "because",
        "been",
        "before",
        "being",
        "between",
        "could",
        "doing",
        "during",
        "other",
        "should",
        "their",
        "there",
        "these",
        "thing",
        "things",
        "those",
        "through",
        "using",
        "where",
        "which",
        "while",
        "would",
        "really",
        "basically",
        "actually",
    }
)


def keywords(text: str) -> list[str]:
    words = re.findall(r"[a-zA-Z][a-zA-Z+#.]{3,}", (text or "").lower())
    seen: set[str] = set()
    result: list[str] = []
    for word in words:
        if word in _STOPWORDS or word in seen:
            continue
        seen.add(word)
        result.append(word)
    return result


async def retrieve_jd_context(answer: str, jd_id: str) -> str:
    """Chunks of the JD most relevant to the candidate's answer.

    Runs the blocking query in a thread so it does not stall the event loop.
    """
    chunks = await asyncio.to_thread(_search, answer, jd_id)
    if not chunks:
        return ""
    return "\n".join(f"- {chunk}" for chunk in chunks)


def _search(answer: str, jd_id: str) -> list[str]:
    db = SessionLocal()
    try:
        terms = keywords(answer)[:6]
        found: list[str] = []
        seen: set[str] = set()

        for term in terms:
            rows = (
                db.query(JDChunk.chunk_text)
                .filter(
                    JDChunk.jd_id == jd_id,
                    JDChunk.chunk_text.ilike(f"%{term}%"),
                )
                .limit(2)
                .all()
            )
            for (chunk_text,) in rows:
                if chunk_text and chunk_text not in seen:
                    seen.add(chunk_text)
                    found.append(chunk_text)
            if len(found) >= settings.TOP_K_JD_CHUNKS:
                break

        if not found:
            found = _first_chunks(db, jd_id)
        return found[: settings.TOP_K_JD_CHUNKS]
    finally:
        db.close()


def _first_chunks(db: Session, jd_id: str) -> list[str]:
    rows = (
        db.query(JDChunk.chunk_text)
        .filter(JDChunk.jd_id == jd_id)
        .order_by(JDChunk.chunk_index)
        .limit(settings.TOP_K_JD_CHUNKS)
        .all()
    )
    return [row[0] for row in rows if row[0]]


def index_jd(db: Session, jd_id: str, jd_text: str) -> int:
    """Split a JD into chunks and store them. Replaces any existing chunks.

    Caller commits. Returns the number of chunks written.
    """
    db.query(JDChunk).filter(JDChunk.jd_id == jd_id).delete(synchronize_session=False)

    words = (jd_text or "").split()
    chunks = [
        " ".join(words[i : i + CHUNK_WORDS]) for i in range(0, len(words), CHUNK_WORDS)
    ]
    for index, chunk_text in enumerate(chunks):
        db.add(JDChunk(jd_id=jd_id, chunk_text=chunk_text, chunk_index=index))

    log.info("jd_indexed", jd_id=jd_id, chunks=len(chunks))
    return len(chunks)

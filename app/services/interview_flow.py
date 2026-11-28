"""Interview session lifecycle: ownership, state recovery, settling, closing.

These were private helpers inside the interviews router. The proctoring router needs the
same ownership check and the same "close this session" path — server-decided termination
has to settle the token and write the integrity summary exactly as a normal end does — so
importing them out of a sibling router would have been the wrong shape.
"""

from __future__ import annotations

import base64
import binascii

from fastapi import HTTPException, status
from sqlalchemy.orm import Session

from app.api.deps import Principal
from app.core.config import settings
from app.core.logging_setup import get_logger
from app.models import (
    InterviewSession,
    SessionScore,
    StudentProfileData,
    utcnow,
)
from app.services import proctoring, session_service, token_service
from app.services.session_service import InterviewState
from app.workers import jobs

log = get_logger("interview_flow")


def owned_active_session(
    db: Session,
    session_id: str,
    principal: Principal,
    *,
    allow_completed: bool = False,
) -> InterviewSession:
    """Fetch a session the caller owns.

    404 either way: a session id belonging to someone else must not be distinguishable
    from one that does not exist.
    """
    session = (
        db.query(InterviewSession).filter(InterviewSession.id == session_id).first()
    )
    if session is None or str(session.student_id) != principal.id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Interview not found")
    if session.status != "active" and not allow_completed:
        raise HTTPException(status.HTTP_409_CONFLICT, "This interview has already ended.")
    return session


async def load_or_rebuild(db: Session, session_id: str) -> InterviewState:
    """Load live state, rebuilding from Postgres if Redis lost it.

    This is what makes a page reload mid-interview survivable; the previous in-memory
    dict lost the session and the student's token with it.
    """
    state = await session_service.load(session_id)
    if state is not None:
        return state

    state = session_service.rebuild_from_db(db, session_id)
    if state is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Interview not found")
    await session_service.create(state)
    return state


def should_consume(state: InterviewState) -> bool:
    """Threshold from PLAN.md §8.2: three answers, or five minutes."""
    return (
        state.answered_count >= settings.TOKEN_CONSUME_AFTER_ANSWERS
        or state.elapsed_seconds >= settings.TOKEN_CONSUME_AFTER_SEC
    )


async def finish_session(
    db: Session,
    session: InterviewSession,
    state: InterviewState,
    *,
    reason: str,
    auto_terminated: bool = False,
) -> None:
    """Close a session: settle the token, write the integrity summary, queue grading."""
    if session.status != "active":
        return

    now = utcnow()
    session.status = "completed"
    session.ended_at = now
    session.ended_reason = reason
    session.duration_seconds = state.elapsed_seconds
    session.answered_count = state.answered_count
    session.proctored = state.proctored

    # Settle the reservation. Below the consume threshold the student keeps the token,
    # because a session that died early is usually our fault, not theirs.
    if not state.token_consumed:
        if reason == "server_error" and token_service.may_auto_release(
            db, state.student_id
        ):
            token_service.release_for_interview(
                db, session_id=state.session_id, reason="Server-side failure"
            )
        elif should_consume(state):
            token_service.consume_for_interview(
                db, session_id=state.session_id, reason=f"Interview ended ({reason})"
            )
        else:
            token_service.release_for_interview(
                db,
                session_id=state.session_id,
                reason=f"Ended before the consume threshold ({reason})",
            )

    summary = proctoring.write_summary(
        db, session_id=state.session_id, auto_terminated=auto_terminated
    )
    session.integrity_score = summary.integrity_score

    vision = state.vision_summary()
    score = (
        db.query(SessionScore).filter(SessionScore.session_id == state.session_id).first()
    )
    if score is None:
        score = SessionScore(session_id=state.session_id)
        db.add(score)
    score.avg_eye_contact_pct = vision["avg_eye_contact"]
    score.avg_posture_score = vision["avg_posture"]
    score.integrity_score = summary.integrity_score
    score.evaluation_status = "pending"
    score.scoring_version = 1

    # Durable, deduplicated and retried — not a fire-and-forget task.
    jobs.enqueue(
        db,
        kind="evaluate_session",
        payload={"session_id": state.session_id},
        dedupe_key=f"evaluate:{state.session_id}",
    )
    db.commit()
    await session_service.delete(state.session_id)
    log.info(
        "interview_finished",
        session_id=state.session_id,
        reason=reason,
        answered=state.answered_count,
    )


def candidate_profile(db: Session, student_id: str) -> str:
    row = (
        db.query(StudentProfileData)
        .filter(StudentProfileData.student_id == student_id)
        .first()
    )
    return (row.scraped_summary or "") if row else ""


def decode_audio(encoded: str) -> bytes | None:
    """Decode the base64 audio blob, refusing anything over the upload limit."""
    try:
        audio = base64.b64decode(encoded, validate=True)
    except (binascii.Error, ValueError):
        log.warning("audio_decode_failed")
        return None
    if len(audio) < 1_000:
        return None
    if len(audio) > settings.MAX_UPLOAD_BYTES:
        log.warning("audio_too_large", size=len(audio))
        return None
    return audio

"""Grade a finished interview.

Moved out of the interviews router: a job handler that only existed as a side effect of
importing an HTTP module was easy to miss and impossible to run without the web layer.

Retried with backoff by the worker. A transcript too short to grade honestly is recorded
as `failed` rather than given a fabricated score — the old code returned a plausible 70
when parsing failed, which is indistinguishable from a real result on the report.
"""

from __future__ import annotations

from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.logging_setup import get_logger
from app.models import (
    InterviewSession,
    SessionScore,
    SessionTranscript,
    utcnow,
)
from app.services import interview_agent
from app.services.jd_context import jd_text_for
from app.workers.jobs import register

log = get_logger("worker.evaluate_session")


@register("evaluate_session")
async def evaluate_session(db: Session, payload: dict) -> None:
    session_id = payload["session_id"]
    session = (
        db.query(InterviewSession).filter(InterviewSession.id == session_id).first()
    )
    score = db.query(SessionScore).filter(SessionScore.session_id == session_id).first()
    if session is None or score is None:  # pragma: no cover
        return
    if score.evaluation_status == "complete":
        return

    turns = (
        db.query(SessionTranscript)
        .filter(SessionTranscript.session_id == session_id)
        .order_by(SessionTranscript.turn_number)
        .all()
    )
    transcript = [(t.turn_number or 0, t.question or "", t.answer or "") for t in turns]
    jd_text, role_title = jd_text_for(db, session)
    vision = {
        "avg_eye_contact": score.avg_eye_contact_pct or 0.0,
        "avg_posture": score.avg_posture_score or 0.0,
        "samples": 1 if score.avg_posture_score else 0,
    }

    try:
        evaluation = await interview_agent.evaluate_transcript(
            session_id=session_id,
            student_id=str(session.student_id),
            role_title=role_title or "this role",
            jd_text=jd_text,
            transcript=transcript,
            vision=vision,
            questions_total=settings.MAX_QUESTIONS,
        )
    except ValueError as exc:
        # Too few answers to grade honestly.
        score.evaluation_status = "failed"
        score.evaluated_at = utcnow()
        db.commit()
        log.info("evaluation_skipped", session_id=session_id, reason=str(exc))
        return

    score.technical_score = evaluation.technical_score
    score.communication_score = evaluation.communication_score
    score.body_language_score = evaluation.body_language_score
    score.overall_score = evaluation.overall_score
    score.verdict = evaluation.verdict
    score.topics_json = [t.model_dump() for t in evaluation.topics]
    score.strengths_json = evaluation.strengths
    score.improvements_json = evaluation.improvements
    score.tips_json = evaluation.tips
    score.evaluation_status = "complete"
    score.evaluated_at = utcnow()
    db.commit()
    log.info(
        "evaluation_stored", session_id=session_id, overall=evaluation.overall_score
    )

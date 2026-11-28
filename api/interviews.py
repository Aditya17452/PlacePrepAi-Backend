"""The interview itself.

Rewritten in Phase 1 for three reasons:

1. **It was unauthenticated.** `POST /api/interviews/start` took `student_id` from the
   request body, and if the student or JD was not found it fell back to
   `db.query(Student).first()` — attributing the interview to an arbitrary account. The
   WebSocket and the report endpoint checked nothing at all. Identity now comes from the
   bearer token and every read is ownership-checked.
2. **Nothing charged for an interview.** No endpoint deducted a credit; the only
   deduction was a client-side call the browser could skip. A token is now reserved
   inside the same transaction that creates the session.
3. **No WebSockets on Vercel** (PLAN.md §0.1). One answer is one `POST .../turns`, made
   idempotent by a client-supplied `turn_id` so a dropped connection does not burn a turn.
"""

from __future__ import annotations

import base64
import binascii
from datetime import datetime

from fastapi import APIRouter, HTTPException, Request, status
from sqlalchemy import func
from sqlalchemy.exc import IntegrityError

from agents import interview_agent
from api.deps import CurrentUser, DbSession, Principal, require_own_or_college
from core import audit, ratelimit
from core.config import settings
from core.llm import LLMBusy, transcribe
from core.logging_setup import get_logger
from models.database import (
    InterviewSession,
    JobDescription,
    SessionScore,
    SessionTranscript,
    Student,
    StudentJD,
    utcnow,
)
from models.proctoring import ProctoringSummary
from models.schemas import (
    EndInterviewRequest,
    FeedbackOut,
    ProctoringBatchRequest,
    ProctoringBatchResponse,
    QuestionBreakdownOut,
    ReportCategories,
    ReportResponse,
    SessionHistoryItem,
    SimpleOk,
    StartInterviewRequest,
    StartInterviewResponse,
    SubmitTurnRequest,
    SubmitTurnResponse,
)
from services import jobs, proctoring, session_service, token_service
from services.rag_service import retrieve_jd_context
from services.session_service import InterviewState

log = get_logger("interviews")

router = APIRouter(prefix="/api/interviews", tags=["interviews"])


# ── Start ─────────────────────────────────────────────────────────────────────


@router.post("/start", response_model=StartInterviewResponse, status_code=201)
async def start_interview(
    body: StartInterviewRequest,
    request: Request,
    principal: CurrentUser,
    db: DbSession,
) -> StartInterviewResponse:
    """Create a session and reserve one token, in a single transaction."""
    await ratelimit.limit_interview_start(request, principal.id)

    student = db.query(Student).filter(Student.id == principal.id).first()
    if student is None:  # pragma: no cover
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Account not found")

    role_title, company = "", ""
    if body.custom_jd_id:
        custom = (
            db.query(StudentJD)
            .filter(
                StudentJD.id == body.custom_jd_id,
                # Ownership: you cannot interview against someone else's pasted JD.
                StudentJD.student_id == principal.id,
            )
            .first()
        )
        if custom is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "That job description was not found")
        role_title, company = custom.role_title, custom.company_name
        jd_id, is_custom = str(custom.id), True
    else:
        jd = (
            db.query(JobDescription)
            .filter(
                JobDescription.id == body.jd_id, JobDescription.is_active.is_(True)
            )
            .first()
        )
        # No "fall back to the first JD in the table" — a missing JD is an error.
        if jd is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "That role was not found")
        role_title, company = jd.role_title, jd.company_name
        jd_id, is_custom = str(jd.id), False

    session = InterviewSession(
        student_id=principal.id,
        jd_id=jd_id,
        is_custom_jd=is_custom,
        status="active",
        proctored=body.proctoring_enabled,
        question_count=1,  # the opening question is asked immediately
        answered_count=0,
    )
    db.add(session)
    db.flush()

    try:
        reservation = token_service.reserve_for_interview(
            db, student=student, session_id=str(session.id), actor_id=principal.id
        )
    except token_service.InsufficientTokens as exc:
        db.rollback()
        raise HTTPException(status.HTTP_402_PAYMENT_REQUIRED, str(exc)) from exc
    except token_service.AllowanceExhausted as exc:
        db.rollback()
        raise HTTPException(status.HTTP_402_PAYMENT_REQUIRED, str(exc)) from exc
    except (
        token_service.DailyCapReached,
        token_service.InterviewAlreadyActive,
    ) as exc:
        db.rollback()
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from exc

    audit.record(
        db,
        action="interview.started",
        actor_id=principal.id,
        actor_role=principal.role.value,
        target=audit.target_for("session", str(session.id)),
        detail={
            "jd_id": jd_id,
            "custom": is_custom,
            "funded_by": reservation.owner_type,
            "proctored": body.proctoring_enabled,
        },
        ip=ratelimit.client_ip(request),
    )
    db.commit()

    opening = interview_agent.opening_question()
    state = InterviewState(
        session_id=str(session.id),
        student_id=principal.id,
        student_name=student.name or "Candidate",
        jd_id=jd_id,
        jd_role=role_title,
        jd_company=company,
        is_custom_jd=is_custom,
        questions_asked=1,
        phase="warmup",
        last_question=opening,
        proctored=body.proctoring_enabled,
    )
    await session_service.create(state)

    log.info(
        "interview_started",
        session_id=str(session.id),
        student_id=principal.id,
        funded_by=reservation.owner_type,
    )
    return StartInterviewResponse(
        sessionId=str(session.id),
        openingQuestion=opening,
        company=company,
        role=role_title,
        maxQuestions=settings.MAX_QUESTIONS,
        maxMinutes=settings.MAX_MINUTES,
        tokens_remaining=reservation.remaining_after,
    )


# ── One turn ──────────────────────────────────────────────────────────────────


@router.post("/{session_id}/turns", response_model=SubmitTurnResponse)
async def submit_turn(
    session_id: str,
    body: SubmitTurnRequest,
    request: Request,
    principal: CurrentUser,
    db: DbSession,
) -> SubmitTurnResponse:
    """Submit one answer and get the next question.

    Replaces the WebSocket `turn_complete` message. Idempotent on `turn_id`: resubmitting
    the same turn returns the same next question instead of advancing the interview.
    """
    session = _owned_active_session(db, session_id, principal)
    state = await _load_or_rebuild(db, session_id)

    # Idempotency: a retry after a dropped request replays the stored response.
    cached = state.handled_turns.get(body.turn_id)
    if cached is not None:
        log.info("turn_replayed", session_id=session_id, turn_id=body.turn_id)
        return SubmitTurnResponse.model_validate(cached)

    await ratelimit.limit_llm(principal.id)

    answer = (body.transcript or "").strip()
    transcript_source = "text" if answer else "none"

    if not answer and body.audio:
        audio_bytes = _decode_audio(body.audio)
        if audio_bytes:
            answer = await transcribe(
                audio=audio_bytes, session_id=session_id, student_id=principal.id
            )
            transcript_source = "whisper" if answer else "none"
    elif answer:
        transcript_source = "browser_stt"

    asked_question = body.answered_question or state.last_question
    state.add_exchange(asked_question, answer or "(no answer recorded)")
    state.answered_count += 1

    jd_text, _ = _jd_text_for(db, session)
    jd_context = await retrieve_jd_context(answer or asked_question, state.jd_id)
    profile = _candidate_profile(db, principal.id)

    try:
        decision = await interview_agent.decide_next_question(
            state=state,
            jd_text=jd_text,
            jd_context=jd_context,
            candidate_profile=profile,
            answer=answer,
        )
    except LLMBusy as exc:
        # Explicit backpressure: the turn is not recorded, so the client can retry the
        # same turn_id without losing the answer or the token.
        log.warning("interview_turn_busy", session_id=session_id)
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, str(exc)) from exc

    # Persist the turn. The unique (session_id, turn_id) index is the backstop if two
    # requests race past the cache check above.
    turn_number = state.answered_count
    db.add(
        SessionTranscript(
            session_id=session_id,
            turn_number=turn_number,
            phase=decision.phase,
            question=asked_question,
            answer=answer or "(no answer recorded)",
            filler_count=interview_agent.count_fillers(answer),
            turn_id=body.turn_id,
            transcript_source=transcript_source,
        )
    )

    consumed = False
    if not state.token_consumed and _should_consume(state):
        consumed = token_service.consume_for_interview(
            db,
            session_id=session_id,
            reason=f"Interview {session_id} reached the consume threshold",
        )
        state.token_consumed = state.token_consumed or consumed

    questions_asked_after = state.questions_asked + (
        1 if decision.question is not None else 0
    )
    session.question_count = questions_asked_after
    session.answered_count = state.answered_count

    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        log.info("turn_duplicate_rejected", session_id=session_id, turn_id=body.turn_id)
        raise HTTPException(
            status.HTTP_409_CONFLICT, "That answer was already submitted."
        ) from None

    feedback: FeedbackOut | None = None
    if (
        decision.question is not None
        and state.answered_count % settings.FEEDBACK_EVERY_N == 0
    ):
        tip = await interview_agent.live_feedback(state, answer)
        if tip is not None:
            feedback = FeedbackOut(tip=tip.tip, tone=tip.tone)  # type: ignore[arg-type]

    if decision.question is not None:
        state.questions_asked = questions_asked_after
        state.last_question = decision.question
        state.phase = decision.phase

    response = SubmitTurnResponse(
        question=decision.question,
        phase=decision.phase,  # type: ignore[arg-type]
        questions_asked=state.questions_asked,
        questions_remaining=max(settings.MAX_QUESTIONS - state.questions_asked, 0),
        session_complete=decision.session_complete,
        feedback=feedback,
        transcript_used=answer,
        token_consumed=consumed,
    )

    # Remember the response for this turn_id, keeping only the recent few.
    state.handled_turns[body.turn_id] = response.model_dump()
    if len(state.handled_turns) > 4:
        for stale_key in list(state.handled_turns)[:-4]:
            state.handled_turns.pop(stale_key, None)
    await session_service.save(state)

    if decision.session_complete:
        await _finish(db, session, state, reason="completed")

    return response


def _should_consume(state: InterviewState) -> bool:
    """Threshold from PLAN.md §8.2: three answers, or five minutes."""
    return (
        state.answered_count >= settings.TOKEN_CONSUME_AFTER_ANSWERS
        or state.elapsed_seconds >= settings.TOKEN_CONSUME_AFTER_SEC
    )


# ── Proctoring ────────────────────────────────────────────────────────────────


@router.post("/{session_id}/proctoring", response_model=ProctoringBatchResponse)
async def submit_proctoring(
    session_id: str,
    body: ProctoringBatchRequest,
    principal: CurrentUser,
    db: DbSession,
) -> ProctoringBatchResponse:
    """Accept a batch of client observations and return the server's decision.

    The client reports; the server decides. Auto-termination only fires on a sustained
    multiple-face or no-face condition (PLAN.md §0 decision 8).
    """
    session = _owned_active_session(db, session_id, principal)
    state = await _load_or_rebuild(db, session_id)

    events = [e.model_dump() for e in body.events]
    stored = proctoring.record_events(db, session_id=session_id, events=events)

    if body.vision is not None:
        state.vision.append(
            {
                "posture_score": body.vision.posture_score,
                "eye_contact_score": body.vision.eye_contact_score,
            }
        )
        # Bound the document: these are only used for an average.
        state.vision = state.vision[-200:]

    decision = proctoring.evaluate(db, session_id=session_id)
    state.warnings_issued = decision.warnings_issued
    db.commit()
    await session_service.save(state)

    if decision.terminate:
        await _finish(
            db, session, state, reason="auto_terminated", auto_terminated=True
        )
        log.info("interview_auto_terminated", session_id=session_id)

    return ProctoringBatchResponse(
        accepted=stored,
        terminate=decision.terminate,
        terminate_reason=decision.reason,
        warnings_issued=decision.warnings_issued,
    )


# ── End ───────────────────────────────────────────────────────────────────────


@router.post("/{session_id}/end", response_model=SimpleOk)
async def end_interview(
    session_id: str,
    body: EndInterviewRequest,
    principal: CurrentUser,
    db: DbSession,
) -> SimpleOk:
    session = _owned_active_session(db, session_id, principal, allow_completed=True)
    if session.status != "active":
        return SimpleOk(ok=True, message="This interview has already ended.")

    state = await _load_or_rebuild(db, session_id)
    await _finish(db, session, state, reason=body.reason)
    return SimpleOk(ok=True, message="Interview ended. Your report is being prepared.")


async def _finish(
    db: DbSession,  # type: ignore[valid-type]
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

    # Settle the reservation. Below the consume threshold the student keeps the token
    # unless they ended it themselves after answering nothing — that is on us, not them.
    if not state.token_consumed:
        if reason == "server_error" and token_service.may_auto_release(
            db, state.student_id
        ):
            token_service.release_for_interview(
                db, session_id=state.session_id, reason="Server-side failure"
            )
        elif _should_consume(state):
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
        db.query(SessionScore)
        .filter(SessionScore.session_id == state.session_id)
        .first()
    )
    if score is None:
        score = SessionScore(session_id=state.session_id)
        db.add(score)
    score.avg_eye_contact_pct = vision["avg_eye_contact"]
    score.avg_posture_score = vision["avg_posture"]
    score.integrity_score = summary.integrity_score
    score.evaluation_status = "pending"
    score.scoring_version = 1

    # Durable, deduplicated, and retried — not a fire-and-forget task.
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


# ── Report & history ──────────────────────────────────────────────────────────


@router.get("/{session_id}/report", response_model=ReportResponse)
async def get_report(
    session_id: str, principal: CurrentUser, db: DbSession
) -> ReportResponse:
    """A report is readable by its owner, their college admin, or a platform admin."""
    session = (
        db.query(InterviewSession).filter(InterviewSession.id == session_id).first()
    )
    if session is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Not found")
    require_own_or_college(principal, str(session.student_id), db)

    if not principal.owns(str(session.student_id)):
        audit.record(
            db,
            action="report.viewed",
            actor_id=principal.id,
            actor_role=principal.role.value,
            target=audit.target_for("session", session_id),
            detail={"student_id": str(session.student_id)},
            commit=True,
        )

    score = (
        db.query(SessionScore).filter(SessionScore.session_id == session_id).first()
    )
    summary = (
        db.query(ProctoringSummary)
        .filter(ProctoringSummary.session_id == session_id)
        .first()
    )

    if score is None or score.evaluation_status == "pending":
        return ReportResponse(
            status="processing",
            integrityScore=summary.integrity_score if summary else None,
            integrityTimeline=(summary.timeline if summary else []) or [],
            proctored=bool(session.proctored),
        )
    if score.evaluation_status == "failed":
        return ReportResponse(
            status="failed",
            integrityScore=summary.integrity_score if summary else None,
            integrityTimeline=(summary.timeline if summary else []) or [],
            proctored=bool(session.proctored),
        )

    turns = (
        db.query(SessionTranscript)
        .filter(SessionTranscript.session_id == session_id)
        .order_by(SessionTranscript.turn_number)
        .all()
    )

    return ReportResponse(
        status="complete",
        overallScore=score.overall_score,
        verdict=score.verdict,
        categories=ReportCategories(
            technicalAccuracy=score.technical_score,
            communication=score.communication_score,
            problemSolving=score.technical_score,
            confidence=score.communication_score,
            bodyLanguage=score.body_language_score,
        ),
        strengths=score.strengths_json or [],
        improvements=score.improvements_json or [],
        tips=score.tips_json or [],
        topics=score.topics_json or [],
        questionBreakdown=[
            QuestionBreakdownOut(
                turn=t.turn_number or 0,
                question=t.question or "",
                answer=t.answer or "",
                phase=t.phase,
            )
            for t in turns
        ],
        summary=f"Overall verdict: {score.verdict}" if score.verdict else None,
        integrityScore=summary.integrity_score if summary else score.integrity_score,
        integrityTimeline=(summary.timeline if summary else []) or [],
        proctored=bool(session.proctored),
        scoringVersion=int(score.scoring_version or 1),
    )


@router.get("/history", response_model=list[SessionHistoryItem])
async def my_history(
    principal: CurrentUser, db: DbSession
) -> list[SessionHistoryItem]:
    return _history_for(db, principal.id)


@router.get("/history/{student_id}", response_model=list[SessionHistoryItem])
async def student_history(
    student_id: str, principal: CurrentUser, db: DbSession
) -> list[SessionHistoryItem]:
    require_own_or_college(principal, student_id, db)
    return _history_for(db, student_id)


def _history_for(db: DbSession, student_id: str) -> list[SessionHistoryItem]:  # type: ignore[valid-type]
    sessions = (
        db.query(InterviewSession)
        .filter(
            InterviewSession.student_id == student_id,
            InterviewSession.status == "completed",
        )
        .order_by(InterviewSession.started_at.desc())
        .limit(100)
        .all()
    )

    items: list[SessionHistoryItem] = []
    for session in sessions:
        score = (
            db.query(SessionScore)
            .filter(SessionScore.session_id == str(session.id))
            .first()
        )
        company, role = _jd_labels(db, session)
        started = session.started_at
        items.append(
            SessionHistoryItem(
                id=str(session.id),
                company=company,
                role=role,
                date=started.strftime("%b %d, %Y")
                if isinstance(started, datetime)
                else "",
                score=score.overall_score if score else None,
                status="Completed",
            )
        )
    return items


# ── Helpers ───────────────────────────────────────────────────────────────────


def _owned_active_session(
    db: DbSession,  # type: ignore[valid-type]
    session_id: str,
    principal: Principal,
    *,
    allow_completed: bool = False,
) -> InterviewSession:
    session = (
        db.query(InterviewSession).filter(InterviewSession.id == session_id).first()
    )
    if session is None or str(session.student_id) != principal.id:
        # 404 either way: a session ID belonging to someone else should not be
        # distinguishable from one that does not exist.
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Interview not found")
    if session.status != "active" and not allow_completed:
        raise HTTPException(
            status.HTTP_409_CONFLICT, "This interview has already ended."
        )
    return session


async def _load_or_rebuild(db: DbSession, session_id: str) -> InterviewState:  # type: ignore[valid-type]
    """Load live state, rebuilding from Postgres if Redis lost it.

    This is what makes a page reload mid-interview survivable, which the in-memory dict
    never allowed.
    """
    state = await session_service.load(session_id)
    if state is not None:
        return state

    state = session_service.rebuild_from_db(db, session_id)
    if state is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Interview not found")
    await session_service.create(state)
    return state


def _jd_text_for(db: DbSession, session: InterviewSession) -> tuple[str, str]:  # type: ignore[valid-type]
    if session.is_custom_jd:
        custom = db.query(StudentJD).filter(StudentJD.id == session.jd_id).first()
        return (custom.jd_text or "", custom.role_title or "") if custom else ("", "")
    jd = db.query(JobDescription).filter(JobDescription.id == session.jd_id).first()
    return (jd.jd_text or "", jd.role_title or "") if jd else ("", "")


def _jd_labels(db: DbSession, session: InterviewSession) -> tuple[str, str]:  # type: ignore[valid-type]
    if session.is_custom_jd:
        custom = db.query(StudentJD).filter(StudentJD.id == session.jd_id).first()
        if custom is not None:
            return custom.company_name or "Custom", custom.role_title or "Role"
    jd = db.query(JobDescription).filter(JobDescription.id == session.jd_id).first()
    if jd is not None:
        return jd.company_name or "Unknown", jd.role_title or "Unknown"
    return "Unknown", "Unknown"


def _candidate_profile(db: DbSession, student_id: str) -> str:  # type: ignore[valid-type]
    from models.database import StudentProfileData

    row = (
        db.query(StudentProfileData)
        .filter(StudentProfileData.student_id == student_id)
        .first()
    )
    return (row.scraped_summary or "") if row else ""


def _decode_audio(encoded: str) -> bytes | None:
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


# Registered here so importing the router registers the handler with the job worker.
@jobs.register("evaluate_session")
async def _evaluate_session_job(db, payload: dict) -> None:  # type: ignore[no-untyped-def]
    """Grade a finished interview. Retried by the worker on failure."""
    session_id = payload["session_id"]
    session = (
        db.query(InterviewSession).filter(InterviewSession.id == session_id).first()
    )
    score = (
        db.query(SessionScore).filter(SessionScore.session_id == session_id).first()
    )
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
    transcript = [
        (t.turn_number or 0, t.question or "", t.answer or "") for t in turns
    ]
    jd_text, role_title = _jd_text_for(db, session)
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
        # Too few answers to grade honestly. Record that, rather than a fabricated score.
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


@router.get("/count", response_model=dict, include_in_schema=False)
async def my_interview_count(principal: CurrentUser, db: DbSession) -> dict:
    """Small helper the dashboard uses for its header counters."""
    total = (
        db.query(func.count(InterviewSession.id))
        .filter(InterviewSession.student_id == principal.id)
        .scalar()
        or 0
    )
    return {"total": int(total)}

"""Proctoring events for an in-progress interview.

Split out of the interviews router because the audience and the trust model are different:
this is the one endpoint whose entire payload is untrusted client observation.

The path stays under `/api/interviews/{session_id}/proctoring` so the client contract does
not change. Events arrive batched over HTTP every few seconds — there is no WebSocket on
Vercel (PLAN.md §0.1).

The client reports; the **server** scores and decides. Auto-termination fires only on a
sustained multiple-face or no-face condition, because the previous client-side rule ("no
face for 5 seconds, end the interview") failed honest candidates on cheap laptops.
"""

from __future__ import annotations

from fastapi import APIRouter

from app.api.deps import CurrentUser, DbSession
from app.core.logging_setup import get_logger
from app.schemas import ProctoringBatchRequest, ProctoringBatchResponse
from app.services import proctoring, session_service
from app.services.interview_flow import (
    finish_session,
    load_or_rebuild,
    owned_active_session,
)

log = get_logger("proctoring_api")

router = APIRouter(prefix="/api/interviews", tags=["proctoring"])


@router.post("/{session_id}/proctoring", response_model=ProctoringBatchResponse)
async def submit_proctoring(
    session_id: str,
    body: ProctoringBatchRequest,
    principal: CurrentUser,
    db: DbSession,
) -> ProctoringBatchResponse:
    """Accept a batch of client observations and return the server's decision."""
    session = owned_active_session(db, session_id, principal)
    state = await load_or_rebuild(db, session_id)

    events = [e.model_dump() for e in body.events]
    stored = proctoring.record_events(db, session_id=session_id, events=events)

    if body.vision is not None:
        state.vision.append(
            {
                "posture_score": body.vision.posture_score,
                "eye_contact_score": body.vision.eye_contact_score,
            }
        )
        # Bound the state document: these are only used for an average.
        state.vision = state.vision[-200:]

    decision = proctoring.evaluate(db, session_id=session_id)
    state.warnings_issued = decision.warnings_issued
    db.commit()
    await session_service.save(state)

    if decision.terminate:
        await finish_session(
            db, session, state, reason="auto_terminated", auto_terminated=True
        )
        log.info("interview_auto_terminated", session_id=session_id)

    return ProctoringBatchResponse(
        accepted=stored,
        terminate=decision.terminate,
        terminate_reason=decision.reason,
        warnings_issued=decision.warnings_issued,
    )

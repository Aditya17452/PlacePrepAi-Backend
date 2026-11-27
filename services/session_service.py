"""Live interview state, in Redis.

This replaces a module-level `_store: dict`. That dict was the single biggest obstacle to
running more than one instance: state lived in whichever process happened to serve the
WebSocket, vanished on every restart and deploy, and made a second worker break sessions
at random. On Vercel, where each request may hit a different short-lived function, it
would not work at all.

State here is a small JSON document per session with a TTL. Postgres remains the durable
record — this is only what the next turn needs in order to continue the conversation.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime
from typing import Any

from core import kv
from core.config import settings
from core.logging_setup import get_logger
from models.database import utcnow

log = get_logger("session_state")

#: Keep the prompt bounded: the last N exchanges go into the next question's context.
MEMORY_WINDOW = 8


def _key(session_id: str) -> str:
    return f"interview:{session_id}"


@dataclass
class Exchange:
    q: str
    a: str


@dataclass
class InterviewState:
    session_id: str
    student_id: str
    student_name: str
    jd_id: str
    jd_role: str
    jd_company: str = ""
    is_custom_jd: bool = False
    questions_asked: int = 0
    answered_count: int = 0
    phase: str = "warmup"
    started_at: str = field(default_factory=lambda: utcnow().isoformat())
    last_question: str = ""
    memory: list[dict[str, str]] = field(default_factory=list)
    vision: list[dict[str, int]] = field(default_factory=list)
    warned_at: str | None = None
    warnings_issued: int = 0
    token_consumed: bool = False
    proctored: bool = True
    #: turn_id -> the response we already returned, so a retry is idempotent.
    handled_turns: dict[str, Any] = field(default_factory=dict)

    # ── Derived ───────────────────────────────────────────────────────────────

    @property
    def elapsed_minutes(self) -> float:
        try:
            started = datetime.fromisoformat(self.started_at)
        except ValueError:  # pragma: no cover - defensive
            return 0.0
        return round((utcnow() - started).total_seconds() / 60, 1)

    @property
    def elapsed_seconds(self) -> int:
        try:
            started = datetime.fromisoformat(self.started_at)
        except ValueError:  # pragma: no cover
            return 0
        return int((utcnow() - started).total_seconds())

    def add_exchange(self, question: str, answer: str) -> None:
        self.memory.append({"q": question, "a": answer})
        self.memory = self.memory[-MEMORY_WINDOW:]

    def asked_questions(self) -> list[str]:
        return [m.get("q", "") for m in self.memory]

    def memory_as_text(self) -> str:
        if not self.memory:
            return "No previous exchanges yet."
        lines: list[str] = []
        for i, exchange in enumerate(self.memory, start=1):
            lines.append(f"Q{i}: {exchange.get('q', '')}")
            lines.append(f"A{i}: {exchange.get('a', '')}")
        return "\n".join(lines)

    def vision_summary(self) -> dict[str, float]:
        """Averages of the webcam heuristics.

        Returns zeros with `samples: 0` when nothing was recorded, rather than the old
        hardcoded 75/80/90 — inventing plausible body-language numbers for a session with
        no webcam data made unproctored sessions look fine.
        """
        if not self.vision:
            return {
                "avg_eye_contact": 0.0,
                "avg_posture": 0.0,
                "attention_pct": 0.0,
                "samples": 0,
            }
        count = len(self.vision)
        return {
            "avg_eye_contact": round(
                sum(v.get("eye_contact_score", 0) for v in self.vision) / count, 1
            ),
            "avg_posture": round(
                sum(v.get("posture_score", 0) for v in self.vision) / count, 1
            ),
            "attention_pct": round(
                sum(1 for v in self.vision if v.get("eye_contact_score", 0) >= 50)
                / count
                * 100,
                1,
            ),
            "samples": count,
        }

    def can_warn(self) -> bool:
        if not self.warned_at:
            return True
        try:
            last = datetime.fromisoformat(self.warned_at)
        except ValueError:  # pragma: no cover
            return True
        return (utcnow() - last).total_seconds() > settings.POSTURE_WARN_COOLDOWN_SEC


# ── Persistence ───────────────────────────────────────────────────────────────


async def create(state: InterviewState) -> InterviewState:
    await kv.set_json(_key(state.session_id), asdict(state), settings.SESSION_TTL_SEC)
    return state


async def load(session_id: str) -> InterviewState | None:
    raw = await kv.get_json(_key(session_id))
    if not raw:
        return None
    try:
        return InterviewState(**raw)
    except TypeError as exc:
        # A schema change left an old document behind; treat it as expired rather than
        # crashing the turn.
        log.warning("interview_state_incompatible", session_id=session_id, error=str(exc))
        return None


async def save(state: InterviewState) -> None:
    """Refreshes the TTL, so an in-progress interview does not expire mid-session."""
    await kv.set_json(_key(state.session_id), asdict(state), settings.SESSION_TTL_SEC)


async def delete(session_id: str) -> None:
    await kv.delete(_key(session_id))


# ── Rebuilding after a cold start ─────────────────────────────────────────────


def rebuild_from_db(db, session_id: str) -> InterviewState | None:  # type: ignore[no-untyped-def]
    """Reconstruct enough state from Postgres to continue an interview.

    Needed when Redis has evicted the document but the session is still open — the
    student reloads the page and should be able to carry on rather than lose the token.
    """
    from models.database import (
        InterviewSession,
        JobDescription,
        SessionTranscript,
        Student,
        StudentJD,
    )

    session = (
        db.query(InterviewSession).filter(InterviewSession.id == session_id).first()
    )
    if session is None:
        return None

    student = db.query(Student).filter(Student.id == session.student_id).first()
    role, company = "Interview", ""
    if session.is_custom_jd:
        custom = db.query(StudentJD).filter(StudentJD.id == session.jd_id).first()
        if custom is not None:
            role, company = custom.role_title, custom.company_name
    else:
        jd = (
            db.query(JobDescription)
            .filter(JobDescription.id == session.jd_id)
            .first()
        )
        if jd is not None:
            role, company = jd.role_title, jd.company_name

    turns = (
        db.query(SessionTranscript)
        .filter(SessionTranscript.session_id == session_id)
        .order_by(SessionTranscript.turn_number)
        .all()
    )

    state = InterviewState(
        session_id=session_id,
        student_id=str(session.student_id),
        student_name=(student.name if student else "Candidate") or "Candidate",
        jd_id=str(session.jd_id),
        jd_role=role,
        jd_company=company,
        is_custom_jd=bool(session.is_custom_jd),
        questions_asked=int(session.question_count or 0),
        answered_count=int(session.answered_count or 0),
        started_at=(session.started_at or utcnow()).isoformat(),
        proctored=bool(session.proctored),
    )
    for turn in turns[-MEMORY_WINDOW:]:
        state.add_exchange(turn.question or "", turn.answer or "")
    if turns:
        state.last_question = turns[-1].question or ""

    log.info("interview_state_rebuilt", session_id=session_id, turns=len(turns))
    return state

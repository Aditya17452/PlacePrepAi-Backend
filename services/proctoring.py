"""Server-side proctoring decisions (PLAN.md §4 Phase 4, §0 decision 8).

Two principles:

1. **The client is not the judge.** The browser reports what it saw; the server decides
   what it means. Previously the page itself ended interviews — a 5-second gap in face
   detection terminated the session, which on a cheap laptop with a stuttering webcam
   fails an honest candidate.
2. **Flag by default.** The only automatic stop is a *sustained* multiple-face or
   no-face condition, with configurable thresholds. Everything else is recorded, scored
   and shown to the student and their college.

The integrity score is deliberately separate from the technical score and is reproducible
from the stored events alone.
"""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy.orm import Session

from core.config import settings
from core.logging_setup import get_logger
from models.database import utcnow
from models.proctoring import ProctoringEvent, ProctoringSummary

log = get_logger("proctoring")

#: Penalty per occurrence, and the cap one event type can contribute. Caps stop a single
#: noisy signal (a flaky webcam firing `no_face` 200 times) from dominating the score.
_WEIGHTS: dict[str, tuple[float, float]] = {
    # event type: (penalty each, maximum total penalty)
    "tab_switch": (6.0, 30.0),
    "window_blur": (3.0, 15.0),
    "fullscreen_exit": (5.0, 20.0),
    "no_face": (2.0, 20.0),
    "multiple_faces": (15.0, 45.0),
    "gaze_away": (1.5, 15.0),
    "face_mismatch": (20.0, 40.0),
    "paste_attempt": (8.0, 24.0),
    "devtools_open": (10.0, 20.0),
    "phone_detected": (15.0, 30.0),
    "second_voice": (10.0, 30.0),
    "mic_muted": (1.0, 5.0),
}

#: Event types that may trigger an automatic stop, and which duration key to read.
_AUTO_STOP = {
    "multiple_faces": "PROCTOR_AUTO_STOP_MULTIFACE_SEC",
    "no_face": "PROCTOR_AUTO_STOP_NOFACE_SEC",
}


@dataclass(frozen=True)
class Decision:
    terminate: bool
    reason: str | None
    warnings_issued: int


def record_events(
    db: Session,
    *,
    session_id: str,
    events: list[dict],
    snapshot_paths: dict[int, str] | None = None,
) -> int:
    """Persist a batch of client-reported events. Caller commits."""
    stored = 0
    for index, event in enumerate(events):
        db.add(
            ProctoringEvent(
                session_id=session_id,
                type=str(event.get("type", "unknown"))[:40],
                severity=int(event.get("severity", 1)),
                detail=event.get("detail"),
                client_ts=event.get("client_ts"),
                server_ts=utcnow(),
                snapshot_path=(snapshot_paths or {}).get(index),
            )
        )
        stored += 1
    return stored


def evaluate(db: Session, *, session_id: str) -> Decision:
    """Decide whether this session should be stopped, from everything stored so far."""
    events = (
        db.query(ProctoringEvent)
        .filter(ProctoringEvent.session_id == session_id)
        .order_by(ProctoringEvent.server_ts)
        .all()
    )

    for event_type, setting_name in _AUTO_STOP.items():
        threshold = int(getattr(settings, setting_name))
        duration = _sustained_duration(events, event_type)
        if duration >= threshold:
            reason = (
                "More than one person was visible for "
                f"{int(duration)} seconds."
                if event_type == "multiple_faces"
                else f"No face was visible for {int(duration)} seconds."
            )
            log.info(
                "proctoring_auto_stop",
                session_id=session_id,
                event_type=event_type,
                duration=duration,
            )
            return Decision(terminate=True, reason=reason, warnings_issued=0)

    # Warnings are informational; they no longer end the interview on their own.
    warnings = sum(1 for e in events if int(e.severity or 1) >= 3)
    return Decision(terminate=False, reason=None, warnings_issued=warnings)


def _sustained_duration(events: list[ProctoringEvent], event_type: str) -> float:
    """Longest continuous run of one condition, in seconds.

    The client reports a condition repeatedly while it holds, each event carrying a
    `duration_ms` where it can measure one. We take the maximum reported duration rather
    than summing occurrences, so a condition that recurs briefly many times does not add
    up to a termination.
    """
    longest = 0.0
    for event in events:
        if event.type != event_type:
            continue
        detail = event.detail or {}
        duration = float(detail.get("duration_ms", 0) or 0) / 1000.0
        longest = max(longest, duration)
    return longest


def integrity_score(db: Session, *, session_id: str) -> tuple[float, list[dict]]:
    """Compute the integrity score and a timeline, from the events alone.

    Starts at 100 and subtracts capped penalties, so the score is explainable: the
    timeline returned here is what the report renders beside it.
    """
    events = (
        db.query(ProctoringEvent)
        .filter(ProctoringEvent.session_id == session_id)
        .order_by(ProctoringEvent.server_ts)
        .all()
    )
    if not events:
        return 100.0, []

    totals: dict[str, float] = {}
    counts: dict[str, int] = {}
    for event in events:
        penalty, cap = _WEIGHTS.get(event.type, (2.0, 10.0))
        # Severity the client assigned scales the penalty a little, but the server's own
        # weight dominates — a client claiming severity 5 on everything gains nothing.
        scaled = penalty * (0.8 + 0.1 * min(int(event.severity or 1), 3))
        totals[event.type] = min(totals.get(event.type, 0.0) + scaled, cap)
        counts[event.type] = counts.get(event.type, 0) + 1

    score = max(0.0, 100.0 - sum(totals.values()))
    timeline = [
        {
            "type": event_type,
            "occurrences": counts[event_type],
            "penalty": round(totals[event_type], 1),
        }
        for event_type in sorted(totals, key=lambda t: -totals[t])
    ]
    return round(score, 1), timeline


def write_summary(
    db: Session, *, session_id: str, auto_terminated: bool
) -> ProctoringSummary:
    """Store the final integrity figure. Caller commits."""
    score, timeline = integrity_score(db, session_id=session_id)
    verdict = (
        "clean"
        if score >= 85
        else "minor_flags"
        if score >= 60
        else "review_recommended"
    )

    summary = (
        db.query(ProctoringSummary)
        .filter(ProctoringSummary.session_id == session_id)
        .first()
    )
    if summary is None:
        summary = ProctoringSummary(session_id=session_id)
        db.add(summary)

    summary.integrity_score = score
    summary.verdict = verdict
    summary.timeline = timeline
    summary.auto_terminated = auto_terminated
    summary.computed_at = utcnow()
    return summary

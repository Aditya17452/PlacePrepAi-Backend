"""Server-side proctoring scoring and the auto-termination policy.

The client reports; the server decides. These tests pin the decision, not the report.
"""

from __future__ import annotations

from app.services import proctoring


def test_integrity_is_100_with_no_events(db):
    score, timeline = proctoring.integrity_score(db, session_id="none")
    assert score == 100.0
    assert timeline == []


def test_penalties_are_capped_per_event_type(db):
    """A flaky webcam firing `no_face` 200 times must not dominate the score."""
    proctoring.record_events(
        db,
        session_id="noisy",
        events=[{"type": "no_face", "severity": 1} for _ in range(200)],
    )
    db.commit()

    score, timeline = proctoring.integrity_score(db, session_id="noisy")
    assert score >= 75, "a single noisy signal is capped"
    assert timeline[0]["type"] == "no_face"
    assert timeline[0]["penalty"] <= 20.0


def test_multiple_faces_is_weighted_heavily(db):
    proctoring.record_events(
        db,
        session_id="cheating",
        events=[{"type": "multiple_faces", "severity": 4} for _ in range(5)],
    )
    db.commit()
    score, _ = proctoring.integrity_score(db, session_id="cheating")
    assert score <= 60


def test_no_auto_termination_without_a_sustained_condition(db):
    proctoring.record_events(
        db,
        session_id="brief",
        events=[
            {"type": "no_face", "severity": 2, "detail": {"duration_ms": 3_000}}
            for _ in range(10)
        ],
    )
    db.commit()
    decision = proctoring.evaluate(db, session_id="brief")
    assert not decision.terminate, "ten brief gaps must not end an interview"


def test_sustained_multiple_faces_terminates(db):
    proctoring.record_events(
        db,
        session_id="sustained",
        events=[
            {
                "type": "multiple_faces",
                "severity": 5,
                "detail": {"duration_ms": 30_000},
            }
        ],
    )
    db.commit()
    decision = proctoring.evaluate(db, session_id="sustained")
    assert decision.terminate
    assert "more than one person" in (decision.reason or "").lower()


def test_client_severity_cannot_dominate_the_score(db):
    """A client claiming severity 5 on a minor event gains little."""
    proctoring.record_events(
        db, session_id="liar", events=[{"type": "mic_muted", "severity": 5}] * 10
    )
    db.commit()
    score, _ = proctoring.integrity_score(db, session_id="liar")
    assert score >= 94

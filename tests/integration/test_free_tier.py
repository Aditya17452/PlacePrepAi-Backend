"""Behaviour that the free tiers depend on.

Supabase Free pauses an idle project; Vercel Hobby fires cron once a day and allows two
jobs; the Groq free key allows very little concurrency. Each of those is handled by a
setting rather than by code, and these tests pin the handling.
"""

from __future__ import annotations

import pytest

from app.core.config import settings
from app.core.llm import LLMBusy, Task, Usage, _pricing
from app.models import JobDescription, JobQueue, SessionScore
from app.services import interview_agent
from app.workers import jobs
from tests.conftest import auth_header, make_student


def _jd(db) -> JobDescription:
    jd = JobDescription(
        company_name="Acme",
        role_title="Backend Engineer",
        jd_text="Python, FastAPI. " * 30,
        is_active=True,
    )
    db.add(jd)
    db.commit()
    db.refresh(jd)
    return jd


def _start(client, student, jd) -> str:
    response = client.post(
        "/api/interviews/start",
        headers=auth_header(student),
        json={"jd_id": str(jd.id), "proctoring_enabled": False},
    )
    assert response.status_code == 201, response.text
    return response.json()["sessionId"]


# ── Groq free tier: backpressure, not a fake answer ──────────────────────────


def test_a_busy_model_returns_503_with_a_readable_message(client, db, monkeypatch):
    student = make_student(db, email="busy@example.com", tokens=3)
    jd = _jd(db)
    session_id = _start(client, student, jd)

    async def busy(**kwargs):
        raise LLMBusy(
            "The interviewer is handling too many sessions right now. "
            "Please retry in a few seconds."
        )

    monkeypatch.setattr(interview_agent, "decide_next_question", busy)

    response = client.post(
        f"/api/interviews/{session_id}/turns",
        headers=auth_header(student),
        json={"turn_id": "turn-0000001", "transcript": "I built a FastAPI service."},
    )
    assert response.status_code == 503, response.text
    detail = response.json()["detail"]
    assert "interviewer" in detail.lower()
    assert "retry" in detail.lower()


def test_a_busy_turn_is_not_recorded_so_the_retry_is_clean(client, db, monkeypatch):
    """The answer must survive backpressure — resubmitting the same turn_id must work."""
    from app.models import SessionTranscript

    student = make_student(db, email="busyretry@example.com", tokens=3)
    jd = _jd(db)
    session_id = _start(client, student, jd)

    async def busy(**kwargs):
        raise LLMBusy("busy")

    monkeypatch.setattr(interview_agent, "decide_next_question", busy)
    client.post(
        f"/api/interviews/{session_id}/turns",
        headers=auth_header(student),
        json={"turn_id": "turn-retry-1", "transcript": "My answer."},
    )

    stored = (
        db.query(SessionTranscript)
        .filter(SessionTranscript.session_id == session_id)
        .count()
    )
    assert stored == 0, "a turn that hit backpressure must not be half-written"

    # The model recovers; the same turn_id now succeeds.
    async def ok(**kwargs):
        return interview_agent.QuestionDecision(
            question="What did you measure?", phase="technical", session_complete=False
        )

    monkeypatch.setattr(interview_agent, "decide_next_question", ok)
    response = client.post(
        f"/api/interviews/{session_id}/turns",
        headers=auth_header(student),
        json={"turn_id": "turn-retry-1", "transcript": "My answer."},
    )
    assert response.status_code == 200, response.text
    assert response.json()["question"] == "What did you measure?"


def test_concurrency_and_llm_rate_limit_are_configurable(monkeypatch):
    """Both knobs that adapt to the Groq plan are settings, not constants."""
    monkeypatch.setattr(settings, "LLM_MAX_CONCURRENCY", 1)
    monkeypatch.setattr(settings, "RATE_LIMIT_LLM", "5/60")
    assert settings.LLM_MAX_CONCURRENCY == 1
    assert settings.RATE_LIMIT_LLM == "5/60"


# ── Cost accounting survives a price change without a deploy ─────────────────


def test_pricing_can_be_overridden_by_env(monkeypatch):
    monkeypatch.setattr(
        settings, "LLM_PRICING_JSON", '{"openai/gpt-oss-120b": [0.20, 0.80]}'
    )
    assert _pricing()["openai/gpt-oss-120b"] == (0.20, 0.80)
    # Models absent from the override keep their built-in price.
    assert _pricing()["openai/gpt-oss-20b"] == (0.075, 0.30)


def test_invalid_pricing_override_falls_back_instead_of_crashing(monkeypatch):
    monkeypatch.setattr(settings, "LLM_PRICING_JSON", "not json at all")
    assert _pricing()["openai/gpt-oss-120b"] == (0.15, 0.60)


def test_cost_is_computed_from_the_override(monkeypatch):
    monkeypatch.setattr(
        settings, "LLM_PRICING_JSON", '{"openai/gpt-oss-120b": [1.0, 2.0]}'
    )
    usage = Usage(
        model="openai/gpt-oss-120b",
        task=Task.QUESTION,
        prompt_tokens=1_000_000,
        completion_tokens=1_000_000,
    )
    assert usage.cost_usd == pytest.approx(3.0)


# ── Vercel Hobby: daily cron, so a pending report drains its own job ─────────


@pytest.mark.asyncio
async def test_drain_one_runs_a_named_job(db):
    ran: list[dict] = []

    @jobs.register("test_marker")
    async def _handler(session, payload):  # noqa: ANN001
        ran.append(payload)

    jobs.enqueue(
        db, kind="test_marker", payload={"x": 1}, dedupe_key="test:marker-1"
    )
    db.commit()

    assert await jobs.drain_one(db, dedupe_key="test:marker-1") is True
    assert ran == [{"x": 1}]
    job = db.query(JobQueue).filter(JobQueue.dedupe_key == "test:marker-1").first()
    assert job.status == "done"

    # A second call finds nothing left to run.
    assert await jobs.drain_one(db, dedupe_key="test:marker-1") is False


@pytest.mark.asyncio
async def test_a_failing_inline_job_goes_back_to_the_queue(db):
    @jobs.register("test_explode")
    async def _handler(session, payload):  # noqa: ANN001
        raise RuntimeError("nope")

    jobs.enqueue(
        db, kind="test_explode", payload={}, dedupe_key="test:explode-1"
    )
    db.commit()

    assert await jobs.drain_one(db, dedupe_key="test:explode-1") is False
    job = db.query(JobQueue).filter(JobQueue.dedupe_key == "test:explode-1").first()
    assert job.status == "queued", "the cron must get another go at it"
    assert job.attempts == 1
    assert "nope" in job.last_error


def test_a_pending_report_drains_its_own_evaluation(client, db, monkeypatch):
    """Otherwise a Hobby deployment leaves the report 'processing' for up to a day."""
    from app.models import InterviewSession

    student = make_student(db, email="report@example.com")
    db.add(
        InterviewSession(
            id="sess-inline",
            student_id=str(student.id),
            jd_id="jd",
            status="completed",
        )
    )
    db.add(SessionScore(session_id="sess-inline", evaluation_status="pending"))
    jobs.enqueue(
        db,
        kind="evaluate_session",
        payload={"session_id": "sess-inline"},
        dedupe_key="evaluate:sess-inline",
    )
    db.commit()

    # Stand in for the LLM: mark the evaluation complete.
    async def fake_handler(session, payload):  # noqa: ANN001
        row = (
            session.query(SessionScore)
            .filter(SessionScore.session_id == payload["session_id"])
            .first()
        )
        row.evaluation_status = "complete"
        row.overall_score = 72
        row.verdict = "Good"
        session.commit()

    monkeypatch.setitem(jobs._HANDLERS, "evaluate_session", fake_handler)

    response = client.get(
        "/api/interviews/sess-inline/report", headers=auth_header(student)
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["status"] == "complete", "the report should not still be processing"
    assert body["overallScore"] == 72


def test_opportunistic_drain_can_be_switched_off(client, db, monkeypatch):
    """On Pro, where cron runs often, the inline path is unnecessary work on a GET."""
    from app.models import InterviewSession

    monkeypatch.setattr(settings, "OPPORTUNISTIC_DRAIN", False)
    student = make_student(db, email="nodrain@example.com")
    db.add(
        InterviewSession(
            id="sess-nodrain",
            student_id=str(student.id),
            jd_id="jd",
            status="completed",
        )
    )
    db.add(SessionScore(session_id="sess-nodrain", evaluation_status="pending"))
    db.commit()

    response = client.get(
        "/api/interviews/sess-nodrain/report", headers=auth_header(student)
    )
    assert response.json()["status"] == "processing"


# ── Supabase Free: the keep-alive ────────────────────────────────────────────


def test_keepalive_requires_the_cron_secret(client, monkeypatch):
    monkeypatch.setattr(settings, "CRON_SECRET", "s3cret")
    assert client.get("/api/cron/keepalive").status_code == 401
    assert (
        client.get(
            "/api/cron/keepalive", headers={"Authorization": "Bearer wrong"}
        ).status_code
        == 401
    )


def test_keepalive_is_disabled_when_no_secret_is_set(client, monkeypatch):
    """Fail closed: an unauthenticated maintenance endpoint is worse than no cron."""
    monkeypatch.setattr(settings, "CRON_SECRET", "")
    assert client.get("/api/cron/keepalive").status_code == 503


def test_keepalive_actually_reaches_the_database(client, monkeypatch):
    """A 200 that never touched Postgres would keep the cron green while it paused."""
    monkeypatch.setattr(settings, "CRON_SECRET", "s3cret")
    response = client.get(
        "/api/cron/keepalive", headers={"Authorization": "Bearer s3cret"}
    )
    assert response.status_code == 200
    assert response.json()["database"] is True

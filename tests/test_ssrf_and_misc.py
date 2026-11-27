"""SSRF guard, config validation, proctoring scoring, and ownership checks."""

from __future__ import annotations

import pytest

from core.roles import Role
from services import proctoring
from services.resume_scraper import (
    UnsafeUrl,
    assert_safe,
    classify,
    extract_urls,
)
from tests.conftest import auth_header, make_student

# ── SSRF ──────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "url",
    [
        "http://169.254.169.254/latest/meta-data/",  # cloud metadata
        "https://169.254.169.254/latest/meta-data/",
        "http://localhost:8000/api/super/stats",
        "https://127.0.0.1/",
        "https://10.0.0.5/internal",
        "file:///etc/passwd",
        "https://evil.example.com/webhook",
        "https://github.com.evil.example.com/user",  # lookalike host
    ],
)
def test_unsafe_urls_are_refused(url):
    with pytest.raises(UnsafeUrl):
        assert_safe(url)


def test_http_is_refused_even_for_an_allowed_host():
    with pytest.raises(UnsafeUrl):
        assert_safe("http://github.com/someone")


def test_allowlisted_host_passes():
    # Hits DNS; github.com resolving to a public address is the thing being asserted.
    assert assert_safe("https://github.com/torvalds") == "github.com"


def test_linkedin_is_classified_but_never_fetched():
    assert classify("https://www.linkedin.com/in/someone") == "linkedin"
    with pytest.raises(UnsafeUrl):
        assert_safe("https://www.linkedin.com/in/someone")


def test_extract_urls_deduplicates_and_trims_punctuation():
    text = (
        "See https://github.com/me, and https://github.com/me again. "
        "Portfolio: https://example.com/page)"
    )
    urls = extract_urls(text)
    assert urls == ["https://github.com/me", "https://example.com/page"]


# ── Configuration validation ──────────────────────────────────────────────────


def test_production_refuses_a_placeholder_jwt_secret(monkeypatch):
    from core.config import Settings

    monkeypatch.setenv("ENV", "production")
    monkeypatch.setenv("JWT_SECRET", "secret")
    monkeypatch.setenv("DATABASE_URL", "postgresql://u:p@host/db")
    with pytest.raises(ValueError, match="JWT_SECRET"):
        Settings(_env_file=None)


def test_production_refuses_sqlite(monkeypatch):
    from core.config import Settings

    monkeypatch.setenv("ENV", "production")
    monkeypatch.setenv("JWT_SECRET", "x" * 40)
    monkeypatch.setenv("DATABASE_URL", "sqlite:///./placeprep.db")
    with pytest.raises(ValueError, match="SQLite"):
        Settings(_env_file=None)


def test_wildcard_cors_is_refused(monkeypatch):
    from core.config import Settings

    monkeypatch.setenv("CORS_ORIGINS", "*")
    monkeypatch.setenv("JWT_SECRET", "x" * 40)
    monkeypatch.setenv("DATABASE_URL", "sqlite:///./x.db")
    with pytest.raises(ValueError, match="CORS_ORIGINS"):
        Settings(_env_file=None)


def test_production_refuses_a_razorpay_test_key(monkeypatch):
    from core.config import Settings

    monkeypatch.setenv("ENV", "production")
    monkeypatch.setenv("JWT_SECRET", "x" * 40)
    monkeypatch.setenv("DATABASE_URL", "postgresql://u:p@host/db")
    monkeypatch.setenv("CORS_ORIGINS", "https://www.placeprepai.fun")
    monkeypatch.setenv("RAZORPAY_KEY_ID", "rzp_test_abc")
    monkeypatch.setenv("RAZORPAY_WEBHOOK_SECRET", "whsec")
    monkeypatch.setenv("UPSTASH_REDIS_REST_URL", "https://x.upstash.io")
    monkeypatch.setenv("UPSTASH_REDIS_REST_TOKEN", "tok")
    monkeypatch.setenv("GROQ_API_KEY", "gsk_x")
    monkeypatch.setenv("ADMIN_BOOTSTRAP_EMAILS", "a@b.com")
    monkeypatch.setenv("SUPABASE_SERVICE_ROLE_KEY", "svc")
    with pytest.raises(ValueError, match="test key"):
        Settings(_env_file=None)


# ── Proctoring ────────────────────────────────────────────────────────────────


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


# ── Ownership ─────────────────────────────────────────────────────────────────


def test_report_of_another_student_is_not_readable(client, db):
    from models.database import InterviewSession, SessionScore

    victim = make_student(db, email="victim@example.com")
    attacker = make_student(db, email="attacker2@example.com")
    db.add(
        InterviewSession(
            id="private-session",
            student_id=str(victim.id),
            jd_id="jd",
            status="completed",
        )
    )
    db.add(
        SessionScore(
            session_id="private-session",
            overall_score=88,
            evaluation_status="complete",
        )
    )
    db.commit()

    assert (
        client.get(
            "/api/interviews/private-session/report", headers=auth_header(victim)
        ).status_code
        == 200
    )
    # 404, not 403: existence must not be confirmed to someone else.
    assert (
        client.get(
            "/api/interviews/private-session/report", headers=auth_header(attacker)
        ).status_code
        == 404
    )


def test_college_admin_cannot_read_another_colleges_student(client, db):
    from tests.conftest import make_college

    college_a = make_college(db, name="A")
    college_b = make_college(db, name="B")
    student_b = make_student(db, college_id=str(college_b.id))
    admin_a = make_student(
        db, role=Role.COLLEGE_ADMIN, college_id=str(college_a.id)
    )

    response = client.get(
        f"/api/interviews/history/{student_b.id}", headers=auth_header(admin_a)
    )
    assert response.status_code == 404


def test_college_admin_can_read_their_own_student(client, db):
    from tests.conftest import make_college

    college = make_college(db, name="Shared")
    student = make_student(db, college_id=str(college.id))
    admin = make_student(db, role=Role.COLLEGE_ADMIN, college_id=str(college.id))

    response = client.get(
        f"/api/interviews/history/{student.id}", headers=auth_header(admin)
    )
    assert response.status_code == 200


# ── Health ────────────────────────────────────────────────────────────────────


def test_health_is_dependency_free(client):
    response = client.get("/health")
    assert response.status_code == 200
    assert response.json()["status"] == "ok"


def test_ready_reports_the_in_memory_fallback_honestly(client):
    response = client.get("/ready")
    assert response.status_code == 200
    body = response.json()
    assert body["database"] is True
    assert "in-memory" in body["detail"].get("redis", "")

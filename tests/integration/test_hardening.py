"""Confirming the Phase 1 hardening claims with tests rather than assertions in a PR.

Each of these was listed as "done" in PLAN.md Phase 1b. None had a test proving it until
now, so a later change could have quietly undone any of them.
"""

from __future__ import annotations

from app.core.config import settings
from app.models import JobDescription
from tests.conftest import auth_header, make_student

PDF_HEADER = b"%PDF-1.4\n"


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


# ── CORS ──────────────────────────────────────────────────────────────────────


def test_configured_origin_is_allowed(client):
    response = client.get(
        "/health", headers={"Origin": "http://localhost:8080"}
    )
    assert response.status_code == 200
    assert response.headers.get("access-control-allow-origin") == "http://localhost:8080"
    assert response.headers.get("access-control-allow-credentials") == "true"


def test_unlisted_origin_is_not_reflected(client):
    """The old config was allow_origins=["*"] with credentials, which is unsafe."""
    response = client.get("/health", headers={"Origin": "https://evil.example.com"})
    assert response.status_code == 200
    assert response.headers.get("access-control-allow-origin") != "https://evil.example.com"
    assert response.headers.get("access-control-allow-origin") != "*"


def test_preflight_from_an_allowed_origin_succeeds(client):
    response = client.options(
        "/api/auth/login",
        headers={
            "Origin": "http://localhost:8080",
            "Access-Control-Request-Method": "POST",
            "Access-Control-Request-Headers": "content-type",
        },
    )
    assert response.status_code == 200
    assert response.headers.get("access-control-allow-origin") == "http://localhost:8080"


# ── Rate limits ───────────────────────────────────────────────────────────────


def test_login_is_rate_limited_per_ip(client):
    """Without this, the generic "invalid email or password" is a free password oracle."""
    limit = int(settings.RATE_LIMIT_AUTH.split("/")[0])
    body = {"email": "nobody@example.com", "password": "whatever-long-enough"}

    codes = [client.post("/api/auth/login", json=body).status_code for _ in range(limit + 2)]
    assert 401 in codes, "the first attempts should be ordinary auth failures"
    assert codes[-1] == 429, f"expected a 429 after {limit} attempts, got {codes}"


def test_rate_limited_response_tells_the_client_when_to_retry(client):
    limit = int(settings.RATE_LIMIT_AUTH.split("/")[0])
    body = {"email": "retry@example.com", "password": "whatever-long-enough"}
    last = None
    for _ in range(limit + 2):
        last = client.post("/api/auth/login", json=body)
    assert last.status_code == 429
    assert last.headers.get("retry-after")


def test_otp_send_is_rate_limited(client, db):
    student = make_student(db, email="otplimit@example.com")
    limit = int(settings.RATE_LIMIT_OTP.split("/")[0])

    codes = []
    for _ in range(limit + 2):
        codes.append(
            client.post(
                "/api/auth/otp/send",
                headers=auth_header(student),
                json={"channel": "email"},
            ).status_code
        )
    assert codes[-1] == 429, f"OTP sends must be capped per destination, got {codes}"


def test_interview_start_is_rate_limited(client, db):
    student = make_student(db, email="startlimit@example.com", tokens=0)
    jd = _jd(db)
    limit = int(settings.RATE_LIMIT_INTERVIEW_START.split("/")[0])

    codes = []
    for _ in range(limit + 2):
        codes.append(
            client.post(
                "/api/interviews/start",
                headers=auth_header(student),
                json={"jd_id": str(jd.id), "proctoring_enabled": False},
            ).status_code
        )
    assert codes[-1] == 429, codes


def test_payment_order_creation_is_rate_limited(client, db):
    student = make_student(db, email="paylimit@example.com")
    codes = []
    for _ in range(12):
        codes.append(
            client.post(
                "/api/payments/create-order",
                headers=auth_header(student),
                json={"plan_code": "pro"},
            ).status_code
        )
    assert 429 in codes, codes


# ── Resume upload limits ──────────────────────────────────────────────────────


def test_upload_requires_authentication(client):
    response = client.post(
        "/api/resume/upload",
        files={"resume": ("cv.pdf", PDF_HEADER + b"x" * 500, "application/pdf")},
    )
    assert response.status_code == 401


def test_oversized_upload_is_refused(client, db):
    student = make_student(db, email="big@example.com")
    oversized = PDF_HEADER + b"x" * (settings.MAX_UPLOAD_BYTES + 1024)

    response = client.post(
        "/api/resume/upload",
        headers=auth_header(student),
        files={"resume": ("huge.pdf", oversized, "application/pdf")},
    )
    assert response.status_code == 413, response.text
    assert "MB" in response.json()["detail"]


def test_non_document_upload_is_refused_on_its_bytes(client, db):
    """The declared content type is not trusted; the magic bytes decide."""
    student = make_student(db, email="magic@example.com")
    response = client.post(
        "/api/resume/upload",
        headers=auth_header(student),
        files={"resume": ("payload.pdf", b"#!/bin/sh\necho hi\n", "application/pdf")},
    )
    assert response.status_code == 400, response.text
    assert "PDF" in response.json()["detail"] or "DOCX" in response.json()["detail"]


def test_empty_upload_is_refused(client, db):
    student = make_student(db, email="empty@example.com")
    response = client.post(
        "/api/resume/upload",
        headers=auth_header(student),
        files={"resume": ("empty.pdf", b"", "application/pdf")},
    )
    assert response.status_code == 400


def test_upload_limit_is_five_megabytes(client):
    assert settings.MAX_UPLOAD_BYTES == 5 * 1024 * 1024


# ── Readiness ─────────────────────────────────────────────────────────────────


def test_ready_checks_the_database_and_reports_redis(client):
    response = client.get("/ready")
    assert response.status_code == 200
    body = response.json()
    assert set(body) == {"status", "database", "redis", "detail"}
    assert body["database"] is True
    # The local fallback is reported as such rather than as a healthy Redis.
    assert "in-memory" in body["detail"]["redis"]


def test_health_does_not_touch_dependencies(client):
    body = client.get("/health").json()
    assert body["status"] == "ok"
    assert "database" not in body

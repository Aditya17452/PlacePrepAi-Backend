"""Consent recording, and the gate it puts on proctored interviews.

PR #2 required proctoring consent in the browser and had nowhere to record it, so the
agreement existed only in the UI. These tests pin the server-side record and the refusal.
"""

from __future__ import annotations

from app.core.config import settings
from app.models import Consent, JobDescription
from app.services import consent as consent_service
from tests.conftest import auth_header, make_student


def _make_jd(db) -> JobDescription:
    jd = JobDescription(
        company_name="Acme",
        role_title="Backend Engineer",
        jd_text="Python, FastAPI, Postgres. " * 20,
        is_active=True,
    )
    db.add(jd)
    db.commit()
    db.refresh(jd)
    return jd


def _current() -> str:
    return consent_service.current_version("proctoring")


# ── Recording ─────────────────────────────────────────────────────────────────


def test_consent_is_stored_with_ip_and_user_agent(client, db):
    student = make_student(db, email="consent@example.com")
    response = client.post(
        "/api/consents",
        headers={**auth_header(student), "User-Agent": "TestAgent/1.0"},
        json={"kind": "proctoring", "version": _current(), "granted": True},
    )
    assert response.status_code == 201, response.text

    row = (
        db.query(Consent)
        .filter(Consent.student_id == str(student.id), Consent.kind == "proctoring")
        .first()
    )
    assert row is not None
    assert row.version == _current()
    assert row.user_agent == "TestAgent/1.0"
    assert row.ip, "the IP is the evidence that this person consented"
    assert row.revoked_at is None


def test_status_reports_whether_consent_is_current(client, db):
    student = make_student(db, email="status@example.com")
    before = client.get("/api/consents", headers=auth_header(student)).json()
    proctoring = next(c for c in before if c["kind"] == "proctoring")
    assert proctoring["is_current"] is False
    assert proctoring["granted_version"] is None

    client.post(
        "/api/consents",
        headers=auth_header(student),
        json={"kind": "proctoring", "version": _current(), "granted": True},
    )
    after = client.get("/api/consents", headers=auth_header(student)).json()
    proctoring = next(c for c in after if c["kind"] == "proctoring")
    assert proctoring["is_current"] is True
    assert proctoring["granted_version"] == _current()


def test_regranting_supersedes_the_previous_consent(client, db):
    student = make_student(db, email="regrant@example.com")
    for _ in range(3):
        client.post(
            "/api/consents",
            headers=auth_header(student),
            json={"kind": "proctoring", "version": _current(), "granted": True},
        )
    live = (
        db.query(Consent)
        .filter(
            Consent.student_id == str(student.id),
            Consent.kind == "proctoring",
            Consent.revoked_at.is_(None),
        )
        .count()
    )
    assert live == 1, "at most one live consent per kind"
    total = (
        db.query(Consent)
        .filter(Consent.student_id == str(student.id), Consent.kind == "proctoring")
        .count()
    )
    assert total == 3, "the history is kept, not overwritten"


def test_withdrawal_is_a_timestamp_not_a_delete(client, db):
    student = make_student(db, email="withdraw@example.com")
    client.post(
        "/api/consents",
        headers=auth_header(student),
        json={"kind": "proctoring", "version": _current(), "granted": True},
    )
    response = client.post(
        "/api/consents",
        headers=auth_header(student),
        json={"kind": "proctoring", "version": _current(), "granted": False},
    )
    assert response.status_code == 201

    rows = (
        db.query(Consent)
        .filter(Consent.student_id == str(student.id), Consent.kind == "proctoring")
        .all()
    )
    assert len(rows) == 1
    assert rows[0].revoked_at is not None, (
        "'consented then withdrew' must stay distinguishable from 'never consented'"
    )
    assert not consent_service.has_current(
        db, student_id=str(student.id), kind="proctoring"
    )


def test_consents_require_authentication(client):
    assert client.get("/api/consents").status_code == 401
    assert (
        client.post(
            "/api/consents", json={"kind": "proctoring", "version": "x", "granted": True}
        ).status_code
        == 401
    )


def test_unknown_consent_kind_is_rejected(client, db):
    student = make_student(db, email="badkind@example.com")
    response = client.post(
        "/api/consents",
        headers=auth_header(student),
        json={"kind": "whatever", "version": _current(), "granted": True},
    )
    assert response.status_code == 422


# ── The gate on /interviews/start ─────────────────────────────────────────────


def test_proctored_interview_refused_without_consent(client, db):
    student = make_student(db, email="noconsent@example.com", tokens=5)
    jd = _make_jd(db)

    response = client.post(
        "/api/interviews/start",
        headers=auth_header(student),
        json={"jd_id": str(jd.id), "proctoring_enabled": True},
    )
    assert response.status_code == 403, response.text
    assert "consent" in response.json()["detail"].lower()


def test_unproctored_interview_does_not_need_consent(client, db):
    """A session with no camera is marked unproctored and needs no camera consent."""
    student = make_student(db, email="unproctored@example.com", tokens=5)
    jd = _make_jd(db)

    response = client.post(
        "/api/interviews/start",
        headers=auth_header(student),
        json={"jd_id": str(jd.id), "proctoring_enabled": False},
    )
    assert response.status_code == 201, response.text


def test_proctored_interview_allowed_after_consent(client, db):
    student = make_student(db, email="withconsent@example.com", tokens=5)
    jd = _make_jd(db)

    client.post(
        "/api/consents",
        headers=auth_header(student),
        json={"kind": "proctoring", "version": _current(), "granted": True},
    )
    response = client.post(
        "/api/interviews/start",
        headers=auth_header(student),
        json={"jd_id": str(jd.id), "proctoring_enabled": True},
    )
    assert response.status_code == 201, response.text
    assert response.json()["tokens_remaining"] == 4


def test_consent_to_an_old_notice_version_does_not_count(client, db):
    """Bumping the notice version must force a fresh agreement, not inherit the old one."""
    student = make_student(db, email="stale@example.com", tokens=5)
    jd = _make_jd(db)

    db.add(
        Consent(
            student_id=str(student.id),
            kind="proctoring",
            version="1999-01-01",
            ip="203.0.113.1",
        )
    )
    db.commit()

    assert not consent_service.has_current(
        db, student_id=str(student.id), kind="proctoring"
    )
    response = client.post(
        "/api/interviews/start",
        headers=auth_header(student),
        json={"jd_id": str(jd.id), "proctoring_enabled": True},
    )
    assert response.status_code == 403


def test_a_withdrawn_consent_blocks_the_next_interview(client, db):
    student = make_student(db, email="revoked@example.com", tokens=5)
    jd = _make_jd(db)

    client.post(
        "/api/consents",
        headers=auth_header(student),
        json={"kind": "proctoring", "version": _current(), "granted": True},
    )
    client.post(
        "/api/consents",
        headers=auth_header(student),
        json={"kind": "proctoring", "version": _current(), "granted": False},
    )
    response = client.post(
        "/api/interviews/start",
        headers=auth_header(student),
        json={"jd_id": str(jd.id), "proctoring_enabled": True},
    )
    assert response.status_code == 403


def test_consent_is_per_student(client, db):
    """One student's consent must not let another start a proctored interview."""
    consenting = make_student(db, email="a@example.com", tokens=5)
    other = make_student(db, email="b@example.com", tokens=5)
    jd = _make_jd(db)

    client.post(
        "/api/consents",
        headers=auth_header(consenting),
        json={"kind": "proctoring", "version": _current(), "granted": True},
    )
    response = client.post(
        "/api/interviews/start",
        headers=auth_header(other),
        json={"jd_id": str(jd.id), "proctoring_enabled": True},
    )
    assert response.status_code == 403


def test_the_gate_is_configurable(client, db, monkeypatch):
    """Turning the requirement off is a config change, not a code change."""
    monkeypatch.setattr(settings, "REQUIRE_PROCTORING_CONSENT", False)
    student = make_student(db, email="gateoff@example.com", tokens=5)
    jd = _make_jd(db)

    response = client.post(
        "/api/interviews/start",
        headers=auth_header(student),
        json={"jd_id": str(jd.id), "proctoring_enabled": True},
    )
    assert response.status_code == 201

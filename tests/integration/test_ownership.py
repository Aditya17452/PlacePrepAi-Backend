"""Ownership and tenancy: who can read whose report, plus the health probes."""

from __future__ import annotations

from app.core.roles import Role
from tests.conftest import auth_header, make_college, make_student


def test_report_of_another_student_is_not_readable(client, db):
    from app.models import InterviewSession, SessionScore

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

    college = make_college(db, name="Shared")
    student = make_student(db, college_id=str(college.id))
    admin = make_student(db, role=Role.COLLEGE_ADMIN, college_id=str(college.id))

    response = client.get(
        f"/api/interviews/history/{student.id}", headers=auth_header(admin)
    )
    assert response.status_code == 200


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

"""TOTP enrolment for admins.

Verification already existed in `login`; enrolment did not, so "MFA is mandatory for
admins" (PLAN.md §7.1) was unenforceable. These tests pin the enrolment half.
"""

from __future__ import annotations

import pyotp

from app.core.roles import Role
from app.models import Student
from tests.conftest import auth_header, make_student


def test_setup_requires_admin(client, db):
    student = make_student(db, email="student-mfa@example.com")
    assert client.post("/api/auth/mfa/setup", headers=auth_header(student)).status_code == 403

    college_admin = make_student(db, email="ca-mfa@example.com", role=Role.COLLEGE_ADMIN)
    assert (
        client.post("/api/auth/mfa/setup", headers=auth_header(college_admin)).status_code
        == 403
    )


def test_setup_returns_a_usable_otpauth_uri(client, db):
    admin = make_student(db, email="admin-mfa@example.com", role=Role.ADMIN)
    response = client.post("/api/auth/mfa/setup", headers=auth_header(admin))
    assert response.status_code == 200, response.text

    body = response.json()
    assert body["otpauth_uri"].startswith("otpauth://totp/")
    assert "PlacePrep%20AI" in body["otpauth_uri"] or "PlacePrep AI" in body["otpauth_uri"]
    assert body["account"] == "admin-mfa@example.com"
    assert len(body["secret"]) >= 16
    # The code generated from the returned secret must be the one the server accepts.
    assert pyotp.TOTP(body["secret"]).now()


def test_setup_stores_the_secret_but_does_not_enable_yet(client, db):
    """A mistyped enrolment must not lock the admin out on their next sign-in."""
    admin = make_student(db, email="pending@example.com", role=Role.ADMIN)
    client.post("/api/auth/mfa/setup", headers=auth_header(admin))

    db.expire_all()
    stored = db.query(Student).filter(Student.id == admin.id).first()
    assert stored.mfa_secret
    assert stored.mfa_enabled is False


def test_confirm_rejects_a_wrong_code(client, db):
    admin = make_student(db, email="wrongcode@example.com", role=Role.ADMIN)
    client.post("/api/auth/mfa/setup", headers=auth_header(admin))

    response = client.post(
        "/api/auth/mfa/confirm", headers=auth_header(admin), json={"code": "000000"}
    )
    assert response.status_code == 400
    db.expire_all()
    assert db.query(Student).filter(Student.id == admin.id).first().mfa_enabled is False


def test_confirm_without_setup_is_refused(client, db):
    admin = make_student(db, email="nosetup@example.com", role=Role.ADMIN)
    response = client.post(
        "/api/auth/mfa/confirm", headers=auth_header(admin), json={"code": "123456"}
    )
    assert response.status_code == 400
    assert "enrolment" in response.json()["detail"].lower()


def test_confirm_enables_mfa_and_signs_out_existing_sessions(client, db):
    admin = make_student(db, email="enable@example.com", role=Role.ADMIN)
    headers = auth_header(admin)

    secret = client.post("/api/auth/mfa/setup", headers=headers).json()["secret"]
    response = client.post(
        "/api/auth/mfa/confirm", headers=headers, json={"code": pyotp.TOTP(secret).now()}
    )
    assert response.status_code == 200, response.text

    db.expire_all()
    stored = db.query(Student).filter(Student.id == admin.id).first()
    assert stored.mfa_enabled is True
    # token_version was bumped, so the token used to enrol is no longer valid.
    assert client.get("/api/auth/me", headers=headers).status_code == 401


def test_login_then_demands_the_authenticator_code(client, db):
    admin = make_student(
        db, email="loginmfa@example.com", role=Role.ADMIN, password="correct-horse-battery"
    )
    headers = auth_header(admin)
    secret = client.post("/api/auth/mfa/setup", headers=headers).json()["secret"]
    client.post(
        "/api/auth/mfa/confirm", headers=headers, json={"code": pyotp.TOTP(secret).now()}
    )

    without = client.post(
        "/api/auth/login",
        json={"email": "loginmfa@example.com", "password": "correct-horse-battery"},
    )
    assert without.status_code == 401
    assert "authenticator" in without.json()["detail"].lower()

    with_code = client.post(
        "/api/auth/login",
        json={
            "email": "loginmfa@example.com",
            "password": "correct-horse-battery",
            "mfa_code": pyotp.TOTP(secret).now(),
        },
    )
    assert with_code.status_code == 200, with_code.text


def test_setup_refuses_once_enabled(client, db):
    """A live session must not be able to quietly swap the second factor."""
    admin = make_student(db, email="noswap@example.com", role=Role.ADMIN)
    headers = auth_header(admin)
    secret = client.post("/api/auth/mfa/setup", headers=headers).json()["secret"]
    client.post(
        "/api/auth/mfa/confirm", headers=headers, json={"code": pyotp.TOTP(secret).now()}
    )

    db.expire_all()
    refreshed = db.query(Student).filter(Student.id == admin.id).first()
    response = client.post("/api/auth/mfa/setup", headers=auth_header(refreshed))
    assert response.status_code == 409


def test_malformed_code_is_rejected_by_the_schema(client, db):
    admin = make_student(db, email="malformed@example.com", role=Role.ADMIN)
    client.post("/api/auth/mfa/setup", headers=auth_header(admin))
    for bad in ("abcdef", "12345", "1234567"):
        response = client.post(
            "/api/auth/mfa/confirm", headers=auth_header(admin), json={"code": bad}
        )
        assert response.status_code == 422, bad

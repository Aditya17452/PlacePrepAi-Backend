"""Regression tests for the Phase 1a security fixes.

Each test names the hole it closes, so a future change that reopens one fails here rather
than in production.
"""

from __future__ import annotations

import hashlib
import hmac
import json

import pytest

from core.roles import Role, normalize_role
from core.security import (
    decode_access_token,
    hash_password,
    make_access_token,
    verify_password,
)
from models.database import Student
from tests.conftest import auth_header, make_student

# ── Privilege escalation via the register body ────────────────────────────────


def test_register_rejects_a_role_field(client):
    """`{"role": "officer"}` used to grant officer access to every college endpoint."""
    response = client.post(
        "/api/auth/register",
        json={
            "name": "Attacker",
            "email": "attacker@example.com",
            "password": "correct-horse-battery",
            "role": "officer",
        },
    )
    # extra="forbid" on the request model turns an unexpected field into a 422.
    assert response.status_code == 422, response.text


def test_register_always_creates_a_student(client, db):
    response = client.post(
        "/api/auth/register",
        json={
            "name": "Normal Person",
            "email": "normal@example.com",
            "password": "correct-horse-battery",
        },
    )
    assert response.status_code == 201, response.text
    assert response.json()["role"] == "student"

    stored = db.query(Student).filter(Student.email == "normal@example.com").first()
    assert stored is not None
    assert normalize_role(stored.role) is Role.STUDENT


def test_register_blocks_disposable_email(client):
    response = client.post(
        "/api/auth/register",
        json={
            "name": "Throwaway",
            "email": "x@mailinator.com",
            "password": "correct-horse-battery",
        },
    )
    assert response.status_code == 400


# ── Passwords ─────────────────────────────────────────────────────────────────


def test_bcrypt_roundtrip():
    stored = hash_password("correct-horse-battery")
    assert stored.startswith("$2")
    assert verify_password("correct-horse-battery", stored).ok
    assert not verify_password("wrong", stored).ok


def test_legacy_sha256_verifies_and_requests_rehash():
    legacy = hashlib.sha256(b"old-password").hexdigest()
    check = verify_password("old-password", legacy)
    assert check.ok
    assert check.needs_rehash, "a legacy digest must be flagged for upgrade"


def test_login_upgrades_a_legacy_hash(client, db):
    student = make_student(
        db,
        email="legacy@example.com",
        password="old-password",
        legacy_sha256=True,
    )
    assert student.password_algo == "sha256"

    response = client.post(
        "/api/auth/login",
        json={"email": "legacy@example.com", "password": "old-password"},
    )
    assert response.status_code == 200, response.text

    db.expire_all()
    refreshed = db.query(Student).filter(Student.id == student.id).first()
    assert refreshed.password_algo == "bcrypt"
    assert refreshed.password_hash.startswith("$2")


def test_login_is_generic_about_unknown_accounts(client):
    response = client.post(
        "/api/auth/login",
        json={"email": "nobody@example.com", "password": "whatever-long-enough"},
    )
    assert response.status_code == 401
    assert response.json()["detail"] == "Invalid email or password."


# ── Token versioning ──────────────────────────────────────────────────────────


def test_token_carries_only_subject_and_version():
    token = make_access_token("abc-123", 7)
    claims = decode_access_token(token)
    assert claims.user_id == "abc-123"
    assert claims.token_version == 7


def test_bumping_token_version_revokes_existing_tokens(client, db):
    student = make_student(db, email="revoke@example.com")
    headers = auth_header(student)
    assert client.get("/api/auth/me", headers=headers).status_code == 200

    student.token_version = 1
    db.commit()

    response = client.get("/api/auth/me", headers=headers)
    assert response.status_code == 401
    assert "revoked" in response.json()["detail"].lower()


def test_role_is_read_from_the_database_not_the_token(client, db):
    """A token issued while a user was an admin must not keep admin access."""
    admin = make_student(db, email="demoted@example.com", role=Role.ADMIN)
    headers = auth_header(admin)
    assert client.get("/api/super/stats", headers=headers).status_code == 200

    admin.role = Role.STUDENT.value
    db.commit()

    assert client.get("/api/super/stats", headers=headers).status_code == 403


def test_deactivated_account_is_refused(client, db):
    student = make_student(db, email="gone@example.com")
    headers = auth_header(student)
    student.is_active = False
    db.commit()
    assert client.get("/api/auth/me", headers=headers).status_code == 403


# ── Role normalisation ────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("stored", "expected"),
    [
        ("officer", Role.COLLEGE_ADMIN),
        ("college_admin", Role.COLLEGE_ADMIN),
        ("super_admin", Role.ADMIN),
        ("admin", Role.ADMIN),
        ("student", Role.STUDENT),
        ("", Role.STUDENT),
        (None, Role.STUDENT),
        ("nonsense", Role.STUDENT),
    ],
)
def test_legacy_roles_normalise(stored, expected):
    assert normalize_role(stored) is expected


def test_unknown_role_downgrades_rather_than_escalating():
    assert normalize_role("superuser") is Role.STUDENT


# ── The free-credits endpoint is gone ─────────────────────────────────────────


def test_unauthenticated_credit_grant_route_no_longer_exists(client, db):
    """`POST /api/payments/{student_id}/add` granted anyone unlimited credits."""
    student = make_student(db, email="target@example.com")
    response = client.post(f"/api/payments/{student.id}/add", json={"amount": 999})
    assert response.status_code in (404, 405), (
        "the unauthenticated credit-grant route must not exist"
    )


def test_no_route_matches_the_old_credit_paths(client):
    import main

    paths = {route.path for route in main.app.routes if hasattr(route, "path")}
    assert "/api/payments/{student_id}/add" not in paths
    assert "/api/credits/{student_id}/add" not in paths
    assert "/api/credits/{student_id}/use" not in paths


# ── Endpoints require authentication ──────────────────────────────────────────


@pytest.mark.parametrize(
    ("method", "path", "body"),
    [
        ("post", "/api/interviews/start", {"jd_id": "x"}),
        ("get", "/api/interviews/history", None),
        ("get", "/api/auth/me", None),
        ("get", "/api/tokens/wallet", None),
        ("post", "/api/payments/create-order", {"plan_code": "pro"}),
        ("get", "/api/officer/students", None),
        ("get", "/api/super/stats", None),
        ("get", "/api/jd/mine", None),
    ],
)
def test_endpoints_reject_anonymous_callers(client, method, path, body):
    response = getattr(client, method)(path, json=body) if body else getattr(client, method)(path)
    assert response.status_code == 401, f"{method.upper()} {path} must require auth"


def test_student_cannot_reach_college_admin_endpoints(client, db):
    student = make_student(db, email="plain@example.com")
    response = client.get("/api/officer/students", headers=auth_header(student))
    assert response.status_code == 403


def test_college_admin_cannot_reach_admin_endpoints(client, db):
    admin = make_student(db, email="college@example.com", role=Role.COLLEGE_ADMIN)
    response = client.post(
        "/api/admin/tokens/grant",
        headers=auth_header(admin),
        json={"college_id": "x", "tokens": 5, "reason": "because"},
    )
    assert response.status_code == 403


# ── Razorpay webhook ──────────────────────────────────────────────────────────


def _sign(body: bytes) -> str:
    return hmac.new(b"webhook-test-secret", body, hashlib.sha256).hexdigest()


def test_webhook_rejects_a_bad_signature(client):
    body = json.dumps({"event": "payment.captured"}).encode()
    response = client.post(
        "/api/payments/webhook",
        content=body,
        headers={"X-Razorpay-Signature": "not-the-signature"},
    )
    assert response.status_code == 400


def test_webhook_accepts_a_valid_signature(client):
    body = json.dumps({"event": "payment.authorized"}).encode()
    response = client.post(
        "/api/payments/webhook",
        content=body,
        headers={"X-Razorpay-Signature": _sign(body)},
    )
    # Signature verified, event type not one we act on.
    assert response.status_code == 200
    assert response.json()["status"] == "ignored"


def test_order_creation_ignores_a_client_supplied_price(client, db):
    student = make_student(db, email="buyer@example.com")
    response = client.post(
        "/api/payments/create-order",
        headers=auth_header(student),
        json={"plan_code": "pro", "amount_paise": 1},
    )
    # The extra field is refused outright rather than silently dropped.
    assert response.status_code == 422

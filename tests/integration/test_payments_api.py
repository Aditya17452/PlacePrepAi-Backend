"""Payments: the deleted free-credit route, webhook signatures, and price tampering."""

from __future__ import annotations

import hashlib
import hmac
import json

from tests.conftest import auth_header, make_student


def test_unauthenticated_credit_grant_route_no_longer_exists(client, db):
    """`POST /api/payments/{student_id}/add` granted anyone unlimited credits."""
    student = make_student(db, email="target@example.com")
    response = client.post(f"/api/payments/{student.id}/add", json={"amount": 999})
    assert response.status_code in (404, 405), (
        "the unauthenticated credit-grant route must not exist"
    )


def test_no_route_matches_the_old_credit_paths(client):
    from app.main import create_app

    paths = {route.path for route in create_app().routes if hasattr(route, "path")}
    assert "/api/payments/{student_id}/add" not in paths
    assert "/api/credits/{student_id}/add" not in paths
    assert "/api/credits/{student_id}/use" not in paths


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

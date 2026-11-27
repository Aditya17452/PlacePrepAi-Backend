"""Password hashing, JWT claims and role normalisation — no database, no HTTP."""

from __future__ import annotations

import hashlib

import pytest

from app.core.roles import Role, normalize_role
from app.core.security import (
    decode_access_token,
    hash_password,
    make_access_token,
    verify_password,
)


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


def test_token_carries_only_subject_and_version():
    token = make_access_token("abc-123", 7)
    claims = decode_access_token(token)
    assert claims.user_id == "abc-123"
    assert claims.token_version == 7


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

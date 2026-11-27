"""Test fixtures.

Environment variables are set before anything imports `core.config`, because the settings
object is built at import time and validates itself.

The database is a file-backed SQLite instance per test (not `:memory:`, so the several
sessions a request opens all see the same data), built from `Base.metadata` rather than by
running migrations — the migrations target Postgres.
"""

from __future__ import annotations

import os
import tempfile
import uuid
from pathlib import Path

os.environ.setdefault("ENV", "local")
os.environ.setdefault("JWT_SECRET", "test-secret-that-is-at-least-32-characters-long")
os.environ.setdefault("ADMIN_BOOTSTRAP_EMAILS", "")
os.environ.setdefault("CORS_ORIGINS", "http://localhost:8080")
os.environ.setdefault("RAZORPAY_KEY_ID", "rzp_test_dummy")
os.environ.setdefault("RAZORPAY_KEY_SECRET", "dummy-secret")
os.environ.setdefault("RAZORPAY_WEBHOOK_SECRET", "webhook-test-secret")
os.environ.setdefault("LOG_LEVEL", "WARNING")
# bcrypt at 12 rounds makes the suite take minutes; the hashing path is identical.
os.environ.setdefault("BCRYPT_ROUNDS", "4")

_db_file = Path(tempfile.gettempdir()) / f"placeprep-test-{uuid.uuid4().hex}.db"
os.environ["DATABASE_URL"] = f"sqlite:///{_db_file.as_posix()}"

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from app.core import kv  # noqa: E402
from app.core.roles import Role  # noqa: E402
from app.core.security import hash_password, make_access_token  # noqa: E402
from app.models import Base, College, SessionLocal, Student, engine  # noqa: E402
from app.services import token_service  # noqa: E402


@pytest.fixture(autouse=True)
def _fresh_schema():
    Base.metadata.drop_all(bind=engine)
    Base.metadata.create_all(bind=engine)
    # The key-value backend is process-global; reset it so rate-limit counters and
    # session state do not leak between tests.
    kv.reset_backend_for_tests(None)
    yield
    Base.metadata.drop_all(bind=engine)


@pytest.fixture
def db():
    session = SessionLocal()
    try:
        yield session
    finally:
        session.close()


@pytest.fixture
def client():
    from app.main import create_app

    # Built per test through the factory, so no module-level app leaks state between them.
    with TestClient(create_app()) as test_client:
        yield test_client


# ── Factories ─────────────────────────────────────────────────────────────────


def make_student(
    db,
    *,
    email: str | None = None,
    role: Role = Role.STUDENT,
    password: str | None = "correct-horse-battery",
    college_id: str | None = None,
    tokens: int = 0,
    verified: bool = True,
    legacy_sha256: bool = False,
) -> Student:
    from datetime import datetime

    if legacy_sha256:
        import hashlib

        password_hash = hashlib.sha256((password or "").encode()).hexdigest()
        algo = "sha256"
    else:
        password_hash = hash_password(password) if password else None
        algo = "bcrypt"

    student = Student(
        name="Test Person",
        email=email or f"{uuid.uuid4().hex[:10]}@example.com",
        role=role.value,
        password_hash=password_hash,
        password_algo=algo,
        college_id=college_id,
        token_version=0,
        is_active=True,
        email_verified_at=datetime.utcnow() if verified else None,
    )
    db.add(student)
    db.flush()

    if tokens:
        token_service.grant(
            db,
            owner_type="student",
            owner_id=str(student.id),
            qty=tokens,
            reason="test fixture",
            actor_id=None,
        )
    else:
        token_service.get_or_create_wallet(db, "student", str(student.id))

    db.commit()
    db.refresh(student)
    return student


def make_college(db, *, name: str = "Test Institute", pool: int = 0) -> College:
    college = College(
        name=name,
        domain=f"{uuid.uuid4().hex[:8]}.edu",
        monthly_allowance_default=4,
        daily_interview_cap=3,
        is_active=True,
    )
    db.add(college)
    db.flush()
    if pool:
        token_service.grant(
            db,
            owner_type="college",
            owner_id=str(college.id),
            qty=pool,
            reason="test fixture",
            actor_id=None,
        )
    else:
        token_service.get_or_create_wallet(db, "college", str(college.id))
    db.commit()
    db.refresh(college)
    return college


def auth_header(student: Student) -> dict[str, str]:
    token = make_access_token(
        str(student.id), int(getattr(student, "token_version", 0) or 0)
    )
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture
def student_factory(db):
    def _make(**kwargs):
        return make_student(db, **kwargs)

    return _make


@pytest.fixture
def college_factory(db):
    def _make(**kwargs):
        return make_college(db, **kwargs)

    return _make

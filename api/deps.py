"""Shared FastAPI dependencies: DB session, authentication, authorisation.

The important rule here (PLAN.md §7.2) is that **authorisation never trusts the request**.
The JWT identifies a user; everything used to make an access decision — role, college,
active flag — is loaded from the database on every request. Callers receive a `Principal`
and must scope their queries with `principal.college_id`, never with an ID from the body,
path or query string.
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass
from typing import Annotated

from fastapi import Depends, HTTPException, Request, status
from sqlalchemy.orm import Session

from core.roles import Role, normalize_role
from core.security import TokenError, decode_access_token
from models.database import SessionLocal, Student


def get_db() -> Iterator[Session]:
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


DbSession = Annotated[Session, Depends(get_db)]


@dataclass(frozen=True)
class Principal:
    """The authenticated caller, as the database currently describes them."""

    id: str
    email: str
    name: str
    role: Role
    college_id: str | None

    @property
    def is_admin(self) -> bool:
        return self.role is Role.ADMIN

    @property
    def is_college_admin(self) -> bool:
        return self.role is Role.COLLEGE_ADMIN

    @property
    def is_student(self) -> bool:
        return self.role is Role.STUDENT

    def owns(self, student_id: str | None) -> bool:
        return student_id is not None and str(student_id) == self.id


def _unauthorized(detail: str) -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail=detail,
        headers={"WWW-Authenticate": "Bearer"},
    )


def _bearer_token(request: Request) -> str:
    header = request.headers.get("Authorization", "")
    scheme, _, token = header.partition(" ")
    if scheme.lower() != "bearer" or not token.strip():
        raise _unauthorized("Missing bearer token")
    return token.strip()


def get_principal(request: Request, db: DbSession) -> Principal:
    """Resolve the caller from the bearer token plus a fresh database read."""
    try:
        claims = decode_access_token(_bearer_token(request))
    except TokenError as exc:
        raise _unauthorized(str(exc)) from exc

    student = db.query(Student).filter(Student.id == claims.user_id).first()
    if student is None:
        raise _unauthorized("Account no longer exists")

    if getattr(student, "is_active", True) is False:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Account is deactivated")

    # Bumping students.token_version invalidates every token issued before the bump.
    current_version = int(getattr(student, "token_version", 0) or 0)
    if claims.token_version != current_version:
        raise _unauthorized("Session has been revoked; please sign in again")

    return Principal(
        id=str(student.id),
        email=student.email or "",
        name=student.name or "",
        role=normalize_role(student.role),
        college_id=str(student.college_id) if student.college_id else None,
    )


CurrentUser = Annotated[Principal, Depends(get_principal)]


def require_roles(*allowed: Role):
    """Dependency factory: allow only the given roles."""

    allowed_set = frozenset(allowed)

    def _guard(principal: CurrentUser) -> Principal:
        if principal.role not in allowed_set:
            raise HTTPException(
                status.HTTP_403_FORBIDDEN,
                f"Requires one of: {', '.join(sorted(r.value for r in allowed_set))}",
            )
        return principal

    return _guard


require_student = require_roles(Role.STUDENT)
require_college_admin = require_roles(Role.COLLEGE_ADMIN, Role.ADMIN)
require_admin = require_roles(Role.ADMIN)

CollegeAdmin = Annotated[Principal, Depends(require_college_admin)]
Admin = Annotated[Principal, Depends(require_admin)]


def require_own_or_college(principal: Principal, student_id: str, db: Session) -> None:
    """Authorise access to one student's data.

    Allowed when the caller *is* that student, is the platform admin, or is a college
    admin and that student belongs to the caller's college. A college admin with no
    college, or a student belonging to a different college, is refused.
    """
    if principal.owns(student_id) or principal.is_admin:
        return

    if principal.is_college_admin and principal.college_id:
        target = db.query(Student).filter(Student.id == student_id).first()
        if target is not None and str(target.college_id or "") == principal.college_id:
            return

    # Deliberately 404, not 403: a college admin should not be able to probe whether a
    # student exists at another college.
    raise HTTPException(status.HTTP_404_NOT_FOUND, "Not found")

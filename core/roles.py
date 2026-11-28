"""The three platform roles, and normalisation of the legacy names.

PLAN.md §7.1: `officer` was renamed to `college_admin` and `super_admin` to `admin`.
Existing rows in `students.role` still hold the legacy values until the data migration
runs, and old JWTs may still carry them, so every read path normalises through
`normalize_role`. Nothing outside this module should compare role strings literally.
"""

from __future__ import annotations

from enum import StrEnum


class Role(StrEnum):
    STUDENT = "student"
    COLLEGE_ADMIN = "college_admin"
    ADMIN = "admin"


# Legacy value -> current value.
_LEGACY = {
    "officer": Role.COLLEGE_ADMIN,
    "college_admin": Role.COLLEGE_ADMIN,
    # `admin` used to be an ambiguous third value sitting between officer and
    # super_admin; the addendum collapses it into the platform-owner role.
    "super_admin": Role.ADMIN,
    "admin": Role.ADMIN,
    "student": Role.STUDENT,
}


def normalize_role(raw: str | None) -> Role:
    """Map any stored or claimed role string onto a current Role.

    Unknown values deliberately fall back to the least privileged role rather than
    raising, so a corrupt row downgrades access instead of breaking the request.
    """
    if not raw:
        return Role.STUDENT
    return _LEGACY.get(raw.strip().lower(), Role.STUDENT)


#: Roles that may read data belonging to a whole college.
COLLEGE_SCOPED = frozenset({Role.COLLEGE_ADMIN, Role.ADMIN})

#: Roles that may move tokens into existence.
CAN_GRANT_TOKENS = frozenset({Role.ADMIN})

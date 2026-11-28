"""Backfill students.college_id, and fill the default allowance group.

Revision ID: 0005_backfill_student_college
Revises: 0004_rls_deny_by_default
Create Date: 2026-11-27

PLAN.md §3 called for making `college_id` the authoritative tenancy column and
backfilling it from the email domain. 0002 added the index but no backfill — so after
0002/0003 every student had `college_id IS NULL`, which breaks two things that used to
work:

* The college-admin console scopes by `college_id` (it used to scope by
  `email LIKE '%@domain'`), so the college would see **zero** students.
* `token_service.college_allowance` needs the student in an allowance group to let them
  draw from the college pool, so nobody could spend the 457 pool tokens.

That is a functional regression against the pre-Phase-1 behaviour, caught by verifying
the migration rather than by the test suite — the tests construct students with an
explicit `college_id`, so they never exercised the backfill.

Matching rule: exact email domain against a college whose `domain_verified_at` is set.
Exact only — `indoreinstitute-edu.com` must never match `indoreinstitute.com`.

Platform admins are deliberately excluded: they own the platform, not a college, and
scoping them to one would narrow what they can see.
"""

from __future__ import annotations

from alembic import op

revision: str = "0005_backfill_student_college"
down_revision: str | None = "0004_rls_deny_by_default"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # 1. Link accounts to the college that owns their verified email domain.
    #    `enrolled_via` records how the link was made, so a later audit can tell a
    #    migrated link from one a student established through the roster.
    op.execute(
        """
        UPDATE students s
        SET college_id = c.id,
            enrolled_via = COALESCE(s.enrolled_via, 'domain')
        FROM colleges c
        WHERE s.college_id IS NULL
          AND s.role <> 'admin'
          AND c.domain IS NOT NULL
          AND c.domain_verified_at IS NOT NULL
          AND c.is_active = TRUE
          AND lower(s.email) LIKE '%@' || lower(c.domain)
        """
    )

    # 2. Put those students into their college's default allowance group.
    #    Only role 'student': a college admin does not sit interviews, so giving them an
    #    interview allowance would be meaningless.
    op.execute(
        """
        INSERT INTO allowance_members (id, group_id, student_id, created_at)
        SELECT gen_random_uuid()::text, g.id, s.id, now()
        FROM students s
        JOIN allowance_groups g
          ON g.college_id = s.college_id AND g.is_default = TRUE
        WHERE s.role = 'student'
          AND s.college_id IS NOT NULL
          AND NOT EXISTS (
            SELECT 1 FROM allowance_members m
            WHERE m.group_id = g.id AND m.student_id = s.id
          )
        """
    )


def downgrade() -> None:
    # Only unlinks what this migration linked. A student who joined through the roster
    # afterwards has enrolled_via = 'roster' and is left alone.
    op.execute(
        """
        DELETE FROM allowance_members m
        USING students s, allowance_groups g
        WHERE m.student_id = s.id
          AND m.group_id = g.id
          AND g.is_default = TRUE
          AND s.enrolled_via = 'domain'
        """
    )
    op.execute(
        "UPDATE students SET college_id = NULL, enrolled_via = NULL "
        "WHERE enrolled_via = 'domain'"
    )

"""Backfill token wallets from the old credit tables, and seed plans.

Revision ID: 0003_backfill_tokens
Revises: 0002_phase1_foundation
Create Date: 2026-11-27

The 10 existing `student_credits` rows are real balances students were given; migrating to
the ledger must not quietly zero them. Each becomes a personal wallet with one
non-expiring `grant` lot and a ledger entry that says where it came from.

The `student_credits` and `credit_transactions` tables are deliberately left in place as
the historical record. Nothing reads them any more.
"""

from __future__ import annotations

import uuid

import sqlalchemy as sa
from alembic import context, op

revision: str = "0003_backfill_tokens"
down_revision: str | None = "0002_phase1_foundation"
branch_labels = None
depends_on = None


def upgrade() -> None:
    if context.is_offline_mode():
        # This migration reads existing rows to decide what to write, so it cannot be
        # rendered as a static script. `alembic upgrade head --sql` is still useful for
        # reviewing 0001 and 0002 before running them.
        raise RuntimeError(
            "0003_backfill_tokens moves data and needs a live connection; "
            "run `alembic upgrade head` against the database instead of --sql."
        )

    conn = op.get_bind()

    # ── Plans ─────────────────────────────────────────────────────────────────
    # Seeded here rather than only in scripts/seed.py so that a migrated database can
    # take a payment without a separate manual step.
    plans = [
        ("free", "student", "Free", 0, 2, None, False, True, True, 0),
        ("starter", "student", "Starter", 14900, 5, 60, False, False, False, 1),
        ("pro", "student", "Pro", 44900, 20, 180, True, False, False, 2),
        (
            "placement_season",
            "student",
            "Placement Season",
            99900,
            50,
            365,
            False,
            False,
            False,
            3,
        ),
        ("college_pilot_free", "college", "Free pilot", 0, 100, 30, False, False, False, 10),
        ("college_pilot", "college", "Pilot", 750000, 500, 365, False, False, False, 11),
        ("college_campus", "college", "Campus", 3000000, 2500, 365, False, False, False, 12),
    ]
    for (
        code,
        audience,
        name,
        price,
        tokens,
        validity,
        recommended,
        requires_phone,
        once_per_phone,
        order,
    ) in plans:
        conn.execute(
            sa.text(
                "INSERT INTO plans (code, audience, name, price_paise, tokens, "
                "validity_days, is_active, is_recommended, requires_phone_verification, "
                "once_per_phone, sort_order, created_at) "
                "VALUES (:code, :audience, :name, :price, :tokens, :validity, TRUE, "
                ":recommended, :requires_phone, :once_per_phone, :order, NOW()) "
                "ON CONFLICT (code) DO NOTHING"
            ),
            {
                "code": code,
                "audience": audience,
                "name": name,
                "price": price,
                "tokens": tokens,
                "validity": validity,
                "recommended": recommended,
                "requires_phone": requires_phone,
                "once_per_phone": once_per_phone,
                "order": order,
            },
        )

    # ── Student wallets from student_credits ──────────────────────────────────
    rows = conn.execute(
        sa.text(
            "SELECT sc.student_id, sc.credits "
            "FROM student_credits sc "
            "JOIN students s ON s.id = sc.student_id "
            "WHERE COALESCE(sc.credits, 0) > 0"
        )
    ).fetchall()

    migrated = 0
    for student_id, credits in rows:
        wallet_id = str(uuid.uuid4())
        conn.execute(
            sa.text(
                "INSERT INTO token_wallets (id, owner_type, owner_id, balance, reserved, "
                "created_at, updated_at) "
                "VALUES (:id, 'student', :owner, :balance, 0, NOW(), NOW()) "
                "ON CONFLICT (owner_type, owner_id) DO NOTHING"
            ),
            {"id": wallet_id, "owner": student_id, "balance": int(credits)},
        )
        actual = conn.execute(
            sa.text(
                "SELECT id FROM token_wallets WHERE owner_type='student' AND owner_id=:o"
            ),
            {"o": student_id},
        ).scalar()
        if actual is None:  # pragma: no cover
            continue

        lot_id = str(uuid.uuid4())
        conn.execute(
            sa.text(
                "INSERT INTO token_lots (id, wallet_id, source, qty, remaining, "
                "expires_at, created_at) "
                "VALUES (:id, :wallet, 'grant', :qty, :qty, NULL, NOW())"
            ),
            {"id": lot_id, "wallet": actual, "qty": int(credits)},
        )
        conn.execute(
            sa.text(
                "INSERT INTO token_ledger (id, wallet_id, lot_id, kind, amount, reason, "
                "on_behalf_of, created_at) "
                "VALUES (:id, :wallet, :lot, 'grant', :qty, "
                "'Migrated from student_credits in 0003_backfill_tokens', :owner, NOW())"
            ),
            {
                "id": str(uuid.uuid4()),
                "wallet": actual,
                "lot": lot_id,
                "qty": int(credits),
                "owner": student_id,
            },
        )
        migrated += 1

    # ── College pools from colleges.total_credits ────────────────────────────
    colleges = conn.execute(
        sa.text(
            "SELECT id, name, COALESCE(total_credits,0) - COALESCE(credits_used,0) "
            "FROM colleges WHERE is_active = TRUE"
        )
    ).fetchall()

    for college_id, name, remaining in colleges:
        wallet_id = str(uuid.uuid4())
        conn.execute(
            sa.text(
                "INSERT INTO token_wallets (id, owner_type, owner_id, balance, reserved, "
                "created_at, updated_at) "
                "VALUES (:id, 'college', :owner, :balance, 0, NOW(), NOW()) "
                "ON CONFLICT (owner_type, owner_id) DO NOTHING"
            ),
            {"id": wallet_id, "owner": college_id, "balance": max(int(remaining), 0)},
        )
        if int(remaining) <= 0:
            continue

        actual = conn.execute(
            sa.text(
                "SELECT id FROM token_wallets WHERE owner_type='college' AND owner_id=:o"
            ),
            {"o": college_id},
        ).scalar()
        lot_id = str(uuid.uuid4())
        conn.execute(
            sa.text(
                "INSERT INTO token_lots (id, wallet_id, source, qty, remaining, "
                "expires_at, created_at) "
                "VALUES (:id, :wallet, 'grant', :qty, :qty, "
                "NOW() + INTERVAL '365 days', NOW())"
            ),
            {"id": lot_id, "wallet": actual, "qty": int(remaining)},
        )
        conn.execute(
            sa.text(
                "INSERT INTO token_ledger (id, wallet_id, lot_id, kind, amount, reason, "
                "created_at) VALUES (:id, :wallet, :lot, 'grant', :qty, "
                "'Migrated from colleges.total_credits in 0003_backfill_tokens', NOW())"
            ),
            {
                "id": str(uuid.uuid4()),
                "wallet": actual,
                "lot": lot_id,
                "qty": int(remaining),
            },
        )

    # ── Default allowance group per college ──────────────────────────────────
    # Without this, enrolled students would have no allowance and could not draw from
    # their college's pool at all. Default is 4 per student per month (PLAN.md §8.3).
    for college_id, name, _ in colleges:
        group_id = str(uuid.uuid4())
        conn.execute(
            sa.text(
                "INSERT INTO allowance_groups (id, college_id, name, filters, "
                "per_student_allowance, period, is_default, created_at) "
                "VALUES (:id, :college, 'All students (default)', '{}', "
                "(SELECT COALESCE(monthly_allowance_default, 4) FROM colleges "
                " WHERE id = :college), 'month', TRUE, NOW())"
            ),
            {"id": group_id, "college": college_id},
        )
        conn.execute(
            sa.text(
                "INSERT INTO allowance_members (id, group_id, student_id, created_at) "
                "SELECT gen_random_uuid()::text, :group, s.id, NOW() FROM students s "
                "WHERE s.college_id = :college"
            ),
            {"group": group_id, "college": college_id},
        )

    print(f"0003_backfill_tokens: migrated {migrated} student wallets, {len(colleges)} colleges")


def downgrade() -> None:
    conn = op.get_bind()
    # Only removes rows this migration created; balances live on in student_credits.
    conn.execute(
        sa.text(
            "DELETE FROM token_ledger WHERE reason LIKE '%0003_backfill_tokens%'"
        )
    )
    conn.execute(sa.text("DELETE FROM allowance_members"))
    conn.execute(sa.text("DELETE FROM allowance_groups WHERE is_default = TRUE"))
    conn.execute(sa.text("DELETE FROM token_lots"))
    conn.execute(sa.text("DELETE FROM token_wallets"))
    conn.execute(sa.text("DELETE FROM plans"))

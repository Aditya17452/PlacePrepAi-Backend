"""Cover the two foreign keys the Supabase performance advisor flagged.

Revision ID: 0006_fk_covering_indexes
Revises: 0005_backfill_student_college
Create Date: 2026-11-27

`token_ledger.lot_id` is the one that matters: `consume_for_interview` and the expiry
sweep both join ledger rows to lots, and without a covering index every FK check and
every lot lookup is a sequential scan over a table that grows once per interview.

`join_codes.college_id` is small and rarely read, but an unindexed FK also makes deleting
a college scan the whole child table, so it is worth the few kilobytes.

The other 29 advisor findings are `unused_index`, which at this point only means no
traffic has reached the new tables yet — every one of those indexes was created minutes
earlier. They are left alone; re-check after the end-to-end run, when "unused" would
actually mean something.
"""

from __future__ import annotations

from alembic import op

revision: str = "0006_fk_covering_indexes"
down_revision: str | None = "0005_backfill_student_college"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_index("ix_token_ledger_lot_id", "token_ledger", ["lot_id"])
    op.create_index("ix_join_codes_college_id", "join_codes", ["college_id"])


def downgrade() -> None:
    op.drop_index("ix_join_codes_college_id", table_name="join_codes")
    op.drop_index("ix_token_ledger_lot_id", table_name="token_ledger")

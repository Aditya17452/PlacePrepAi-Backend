"""Enable RLS deny-by-default on every public table.

Revision ID: 0004_rls_deny_by_default
Revises: 0003_backfill_tokens
Create Date: 2026-11-27

The Supabase security advisor reports 9 tables with RLS disabled, including `students`,
`session_transcripts` and `session_scores`. With RLS off, anyone holding the project's
**anon key** can read or write every row through PostgREST — and the anon key ships to
browsers by design.

The fix here is deliberately blunt: enable RLS and create **no policies**. No policy means
no access for `anon` and `authenticated`, which is correct for this project because the
browser never talks to Supabase directly — the frontend has no `@supabase/supabase-js`
dependency and no Supabase call anywhere. Every read goes through the FastAPI backend,
which connects through the pooler as the role that owns these tables — and a table owner
is exempt from RLS unless it is FORCEd, which is why it is not.

Grants are revoked as well, so the denial does not rest on RLS alone.

Deliberately **not** `FORCE ROW LEVEL SECURITY`: that would subject the table owner to
RLS too, and the backend connects as the owner through the pooler. With no policies
defined, forcing it would deny the application everything — the outage would be total and
would look like a database failure rather than a permissions change.

If a future feature does need direct client access, that feature adds its own policies in
its own migration — which is the point of starting from deny.
"""

from __future__ import annotations

from alembic import op

revision: str = "0004_rls_deny_by_default"
down_revision: str | None = "0003_backfill_tokens"
branch_labels = None
depends_on = None

# Every table this application owns in the public schema.
TABLES = (
    # Pre-existing
    "students",
    "colleges",
    "job_descriptions",
    "jd_chunks",
    "interview_sessions",
    "session_transcripts",
    "session_scores",
    "shortlists",
    "student_jds",
    "student_credits",
    "credit_transactions",
    "student_profile_data",
    # Added in 0002
    "token_wallets",
    "token_lots",
    "token_ledger",
    "allowance_groups",
    "allowance_members",
    "allowance_overrides",
    "plans",
    "payments",
    "idempotency_keys",
    "llm_usage",
    "job_queue",
    "verification_codes",
    "roster_entries",
    "join_codes",
    "enrollment_requests",
    "consents",
    "api_audit_log",
    "proctoring_events",
    "proctoring_summaries",
    # Alembic's own bookkeeping is in the public schema too and is just as readable.
    "alembic_version",
)

# `anon` and `authenticated` exist on Supabase but not on a plain Postgres, so the
# revoke is wrapped in a check rather than failing the migration elsewhere.
_REVOKE = """
DO $$
DECLARE
    r text;
BEGIN
    FOREACH r IN ARRAY ARRAY['anon', 'authenticated'] LOOP
        IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = r) THEN
            EXECUTE format('REVOKE ALL ON TABLE public.%I FROM %I', '{table}', r);
        END IF;
    END LOOP;
END $$;
"""

_GRANT = """
DO $$
DECLARE
    r text;
BEGIN
    FOREACH r IN ARRAY ARRAY['anon', 'authenticated'] LOOP
        IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = r) THEN
            EXECUTE format('GRANT ALL ON TABLE public.%I TO %I', '{table}', r);
        END IF;
    END LOOP;
END $$;
"""


def upgrade() -> None:
    for table in TABLES:
        # IF EXISTS: `student_credits` and friends are legacy and present, but a fresh
        # environment built from migrations alone should not fail on an absent table.
        op.execute(
            f"DO $$ BEGIN "
            f"IF EXISTS (SELECT 1 FROM pg_tables WHERE schemaname='public' "
            f"AND tablename='{table}') THEN "
            f"EXECUTE 'ALTER TABLE public.{table} ENABLE ROW LEVEL SECURITY'; "
            f"END IF; END $$;"
        )
        op.execute(_REVOKE.replace("{table}", table))


def downgrade() -> None:
    for table in TABLES:
        op.execute(
            f"DO $$ BEGIN "
            f"IF EXISTS (SELECT 1 FROM pg_tables WHERE schemaname='public' "
            f"AND tablename='{table}') THEN "
            f"EXECUTE 'ALTER TABLE public.{table} DISABLE ROW LEVEL SECURITY'; "
            f"END IF; END $$;"
        )
        op.execute(_GRANT.replace("{table}", table))

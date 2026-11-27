"""Phase 1: auth hardening, tokens, payments, identity, proctoring, jobs.

Revision ID: 0002_phase1_foundation
Revises: 0001_baseline
Create Date: 2026-11-27

Additive except for two things, both called out in PLAN.md and approved:

* `student_credits.updated_at` is cast from TEXT to TIMESTAMP (10 rows).
* Legacy role values are renamed: `officer` -> `college_admin`,
  `super_admin` -> `admin`. `core/roles.py` still normalises the old values, so a
  half-applied state cannot lock anyone out.

The credits tables are **kept**, not dropped: they are the audit trail of what students
were given before the token ledger existed, and `0003` backfills from them.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "0002_phase1_foundation"
down_revision: str | None = "0001_baseline"
branch_labels = None
depends_on = None

TS = sa.DateTime()


def upgrade() -> None:
    # ── students: verification, password algorithm, session invalidation ───────
    with op.batch_alter_table("students") as batch:
        batch.add_column(sa.Column("phone", sa.String(20), nullable=True))
        batch.add_column(sa.Column("email_verified_at", TS, nullable=True))
        batch.add_column(sa.Column("phone_verified_at", TS, nullable=True))
        batch.add_column(
            sa.Column(
                "password_algo",
                sa.String(20),
                nullable=False,
                server_default="sha256",
            )
        )
        batch.add_column(
            sa.Column(
                "token_version", sa.Integer(), nullable=False, server_default="0"
            )
        )
        batch.add_column(
            sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.true())
        )
        batch.add_column(sa.Column("deleted_at", TS, nullable=True))
        batch.add_column(sa.Column("mfa_secret", sa.String(64), nullable=True))
        batch.add_column(
            sa.Column(
                "mfa_enabled", sa.Boolean(), nullable=False, server_default=sa.false()
            )
        )
        batch.add_column(sa.Column("enrolled_via", sa.String(20), nullable=True))
        batch.add_column(sa.Column("updated_at", TS, nullable=True))

    op.create_unique_constraint("uq_students_phone", "students", ["phone"])
    # The FK on students.college_id had no covering index — flagged by the Supabase
    # performance advisor, and it is now the tenancy column for every officer query.
    op.create_index("ix_students_college_id", "students", ["college_id"])

    # Existing rows were hashed with unsalted SHA-256; mark them so login upgrades them.
    op.execute("UPDATE students SET password_algo = 'sha256' WHERE password_hash IS NOT NULL")
    op.execute(
        "UPDATE students SET password_algo = 'google_oauth' "
        "WHERE password_hash = 'google_oauth'"
    )
    op.execute("UPDATE students SET email_verified_at = created_at WHERE role <> 'student'")

    # Role rename (PLAN.md §0.2).
    op.execute("UPDATE students SET role = 'college_admin' WHERE role = 'officer'")
    op.execute("UPDATE students SET role = 'admin' WHERE role = 'super_admin'")

    # ── colleges: distribution controls ───────────────────────────────────────
    with op.batch_alter_table("colleges") as batch:
        batch.add_column(sa.Column("domain_verified_at", TS, nullable=True))
        batch.add_column(
            sa.Column(
                "monthly_allowance_default",
                sa.Integer(),
                nullable=False,
                server_default="4",
            )
        )
        batch.add_column(
            sa.Column(
                "daily_interview_cap", sa.Integer(), nullable=False, server_default="3"
            )
        )
        batch.add_column(
            sa.Column(
                "distribution_frozen",
                sa.Boolean(),
                nullable=False,
                server_default=sa.false(),
            )
        )
        batch.add_column(
            sa.Column(
                "snapshot_sharing_allowed",
                sa.Boolean(),
                nullable=False,
                server_default=sa.false(),
            )
        )
        batch.add_column(sa.Column("deleted_at", TS, nullable=True))
        batch.add_column(sa.Column("updated_at", TS, nullable=True))

    # The one college already enrolled was verified out of band, so keep it working.
    op.execute(
        "UPDATE colleges SET domain_verified_at = created_at WHERE domain IS NOT NULL"
    )

    # ── interviews ────────────────────────────────────────────────────────────
    with op.batch_alter_table("interview_sessions") as batch:
        batch.add_column(
            sa.Column(
                "is_custom_jd", sa.Boolean(), nullable=False, server_default=sa.false()
            )
        )
        batch.add_column(
            sa.Column(
                "proctored", sa.Boolean(), nullable=False, server_default=sa.true()
            )
        )
        batch.add_column(sa.Column("integrity_score", sa.Float(), nullable=True))
        batch.add_column(sa.Column("ended_reason", sa.String(30), nullable=True))
        batch.add_column(
            sa.Column("plan_version", sa.Integer(), nullable=False, server_default="1")
        )
        batch.add_column(
            sa.Column(
                "answered_count", sa.Integer(), nullable=False, server_default="0"
            )
        )

    # A session whose jd_id is in student_jds was a custom-JD interview.
    op.execute(
        "UPDATE interview_sessions SET is_custom_jd = TRUE "
        "WHERE jd_id IN (SELECT id FROM student_jds)"
    )
    op.execute(
        "UPDATE interview_sessions SET answered_count = COALESCE(("
        " SELECT COUNT(*) FROM session_transcripts t "
        " WHERE t.session_id = interview_sessions.id), 0)"
    )

    op.create_index(
        "ix_interview_sessions_student_started",
        "interview_sessions",
        ["student_id", "started_at"],
    )
    op.create_index("ix_interview_sessions_jd", "interview_sessions", ["jd_id"])

    with op.batch_alter_table("session_transcripts") as batch:
        batch.add_column(sa.Column("turn_id", sa.String(64), nullable=True))
        batch.add_column(sa.Column("transcript_source", sa.String(20), nullable=True))

    # Every report read scans this table; it had no index on session_id at all.
    op.create_index(
        "ix_session_transcripts_session_turn",
        "session_transcripts",
        ["session_id", "turn_number"],
    )
    op.create_index(
        "uq_session_transcripts_turn_id",
        "session_transcripts",
        ["session_id", "turn_id"],
        unique=True,
    )
    op.create_index("ix_jd_chunks_jd_id", "jd_chunks", ["jd_id"])
    op.create_index(
        "ix_credit_transactions_student", "credit_transactions", ["student_id", "created_at"]
    )

    with op.batch_alter_table("session_scores") as batch:
        batch.add_column(sa.Column("competency_scores", sa.JSON(), nullable=True))
        batch.add_column(sa.Column("weighted_total", sa.Float(), nullable=True))
        batch.add_column(sa.Column("integrity_score", sa.Float(), nullable=True))
        batch.add_column(
            sa.Column(
                "scoring_version", sa.Integer(), nullable=False, server_default="1"
            )
        )

    with op.batch_alter_table("job_descriptions") as batch:
        batch.add_column(sa.Column("requirements_extracted_at", TS, nullable=True))

    # ── student_credits.updated_at: TEXT -> TIMESTAMP (approved, 10 rows) ─────
    op.execute(
        "ALTER TABLE student_credits "
        "ALTER COLUMN updated_at TYPE TIMESTAMP "
        "USING NULLIF(updated_at, '')::timestamp"
    )

    # ── Tokens ────────────────────────────────────────────────────────────────
    op.create_table(
        "token_wallets",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("owner_type", sa.String(10), nullable=False),
        sa.Column("owner_id", sa.String(36), nullable=False),
        sa.Column("balance", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("reserved", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("created_at", TS, nullable=True),
        sa.Column("updated_at", TS, nullable=True),
        sa.UniqueConstraint("owner_type", "owner_id", name="uq_token_wallets_owner"),
        sa.CheckConstraint("balance >= 0", name="ck_token_wallets_balance_non_negative"),
        sa.CheckConstraint("reserved >= 0", name="ck_token_wallets_reserved_non_negative"),
        sa.CheckConstraint(
            "owner_type in ('student','college')", name="ck_token_wallets_owner_type"
        ),
    )

    op.create_table(
        "token_lots",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column(
            "wallet_id", sa.String(36), sa.ForeignKey("token_wallets.id"), nullable=False
        ),
        sa.Column("source", sa.String(20), nullable=False),
        sa.Column("qty", sa.Integer(), nullable=False),
        sa.Column("remaining", sa.Integer(), nullable=False),
        sa.Column("expires_at", TS, nullable=True),
        sa.Column("payment_id", sa.String(64), nullable=True),
        sa.Column("plan_code", sa.String(40), nullable=True),
        sa.Column("expiry_notified_at", TS, nullable=True),
        sa.Column("created_at", TS, nullable=True),
        sa.CheckConstraint("remaining >= 0", name="ck_token_lots_remaining_non_negative"),
        sa.CheckConstraint("remaining <= qty", name="ck_token_lots_remaining_lte_qty"),
    )
    op.create_index("ix_token_lots_wallet_id", "token_lots", ["wallet_id"])
    op.create_index("ix_token_lots_wallet_expiry", "token_lots", ["wallet_id", "expires_at"])
    op.create_index("ix_token_lots_payment_id", "token_lots", ["payment_id"])

    op.create_table(
        "token_ledger",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column(
            "wallet_id", sa.String(36), sa.ForeignKey("token_wallets.id"), nullable=False
        ),
        sa.Column("lot_id", sa.String(36), sa.ForeignKey("token_lots.id"), nullable=True),
        sa.Column("kind", sa.String(20), nullable=False),
        sa.Column("amount", sa.Integer(), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("actor_id", sa.String(36), nullable=True),
        sa.Column("session_id", sa.String(36), nullable=True),
        sa.Column("payment_id", sa.String(64), nullable=True),
        sa.Column("on_behalf_of", sa.String(36), nullable=True),
        sa.Column("created_at", TS, nullable=True),
        sa.CheckConstraint(
            "kind in ('grant','purchase','reserve','release','consume','refund',"
            "'expire','revoke','adjust')",
            name="ck_token_ledger_kind",
        ),
    )
    op.create_index("ix_token_ledger_wallet_id", "token_ledger", ["wallet_id"])
    op.create_index("ix_token_ledger_session_id", "token_ledger", ["session_id"])
    op.create_index("ix_token_ledger_payment_id", "token_ledger", ["payment_id"])
    op.create_index("ix_token_ledger_created_at", "token_ledger", ["created_at"])
    # One reservation per interview: a retried start cannot double-reserve.
    op.execute(
        "CREATE UNIQUE INDEX uq_token_ledger_session_reserve "
        "ON token_ledger (session_id) WHERE kind = 'reserve'"
    )

    op.create_table(
        "allowance_groups",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column(
            "college_id", sa.String(36), sa.ForeignKey("colleges.id"), nullable=False
        ),
        sa.Column("name", sa.String(200), nullable=False),
        sa.Column("filters", sa.JSON(), nullable=True),
        sa.Column("per_student_allowance", sa.Integer(), nullable=False),
        sa.Column("period", sa.String(10), nullable=False, server_default="month"),
        sa.Column("valid_from", TS, nullable=True),
        sa.Column("valid_until", TS, nullable=True),
        sa.Column("is_default", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("created_by", sa.String(36), nullable=True),
        sa.Column("created_at", TS, nullable=True),
        sa.CheckConstraint(
            "per_student_allowance >= 0", name="ck_allowance_groups_non_negative"
        ),
        sa.CheckConstraint("period in ('month','term')", name="ck_allowance_groups_period"),
    )
    op.create_index("ix_allowance_groups_college", "allowance_groups", ["college_id"])

    op.create_table(
        "allowance_members",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column(
            "group_id",
            sa.String(36),
            sa.ForeignKey("allowance_groups.id"),
            nullable=False,
        ),
        sa.Column("student_id", sa.String(36), nullable=False),
        sa.Column("created_at", TS, nullable=True),
        sa.UniqueConstraint("group_id", "student_id", name="uq_allowance_members"),
    )
    op.create_index("ix_allowance_members_student", "allowance_members", ["student_id"])

    op.create_table(
        "allowance_overrides",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column(
            "college_id", sa.String(36), sa.ForeignKey("colleges.id"), nullable=False
        ),
        sa.Column("student_id", sa.String(36), nullable=False),
        sa.Column("kind", sa.String(10), nullable=False, server_default="topup"),
        sa.Column("extra_allowance", sa.Integer(), nullable=False),
        sa.Column("label", sa.String(200), nullable=True),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("jd_id", sa.String(36), nullable=True),
        sa.Column("valid_from", TS, nullable=True),
        sa.Column("valid_until", TS, nullable=True),
        sa.Column("created_by", sa.String(36), nullable=True),
        sa.Column("created_at", TS, nullable=True),
        sa.CheckConstraint(
            "extra_allowance >= 0", name="ck_allowance_overrides_non_negative"
        ),
        sa.CheckConstraint("kind in ('topup','drive')", name="ck_allowance_overrides_kind"),
    )
    op.create_index(
        "ix_allowance_overrides_student", "allowance_overrides", ["student_id"]
    )
    op.create_index(
        "ix_allowance_overrides_college", "allowance_overrides", ["college_id"]
    )

    # ── Billing ───────────────────────────────────────────────────────────────
    op.create_table(
        "plans",
        sa.Column("code", sa.String(40), primary_key=True),
        sa.Column("audience", sa.String(10), nullable=False, server_default="student"),
        sa.Column("name", sa.String(120), nullable=False),
        sa.Column("price_paise", sa.Integer(), nullable=False),
        sa.Column("tokens", sa.Integer(), nullable=False),
        sa.Column("validity_days", sa.Integer(), nullable=True),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column(
            "is_recommended", sa.Boolean(), nullable=False, server_default=sa.false()
        ),
        sa.Column(
            "requires_phone_verification",
            sa.Boolean(),
            nullable=False,
            server_default=sa.false(),
        ),
        sa.Column(
            "once_per_phone", sa.Boolean(), nullable=False, server_default=sa.false()
        ),
        sa.Column("sort_order", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("notes", sa.Text(), nullable=True),
        sa.Column("created_at", TS, nullable=True),
        sa.Column("updated_at", TS, nullable=True),
        sa.CheckConstraint("price_paise >= 0", name="ck_plans_price_non_negative"),
        sa.CheckConstraint("tokens >= 0", name="ck_plans_tokens_non_negative"),
        sa.CheckConstraint("audience in ('student','college')", name="ck_plans_audience"),
    )

    # This table never existed, which is why /payments/verify rolled back every grant.
    op.create_table(
        "payments",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("student_id", sa.String(36), nullable=False),
        sa.Column("plan_code", sa.String(40), nullable=False),
        sa.Column("order_id", sa.String(64), nullable=False, unique=True),
        sa.Column("payment_id", sa.String(64), nullable=True, unique=True),
        sa.Column("amount_paise", sa.Integer(), nullable=False),
        sa.Column("currency", sa.String(8), nullable=False, server_default="INR"),
        sa.Column("status", sa.String(24), nullable=False, server_default="created"),
        sa.Column("tokens_granted", sa.Integer(), nullable=False, server_default="0"),
        sa.Column(
            "signature_verified", sa.Boolean(), nullable=False, server_default=sa.false()
        ),
        sa.Column("raw_webhook", sa.JSON(), nullable=True),
        sa.Column("refunded_paise", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("invoice_no", sa.String(40), nullable=True, unique=True),
        sa.Column("gst_paise", sa.Integer(), nullable=True),
        sa.Column("created_at", TS, nullable=True),
        sa.Column("captured_at", TS, nullable=True),
        sa.Column("updated_at", TS, nullable=True),
        sa.CheckConstraint(
            "status in ('created','captured','failed','refunded','partially_refunded')",
            name="ck_payments_status",
        ),
        sa.CheckConstraint("amount_paise >= 0", name="ck_payments_amount_non_negative"),
    )
    op.create_index("ix_payments_student_created", "payments", ["student_id", "created_at"])

    op.create_table(
        "idempotency_keys",
        sa.Column("key", sa.String(128), primary_key=True),
        sa.Column("scope", sa.String(60), nullable=False),
        sa.Column("actor_id", sa.String(36), nullable=True),
        sa.Column("response", sa.JSON(), nullable=True),
        sa.Column("created_at", TS, nullable=True),
    )

    op.create_table(
        "llm_usage",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("session_id", sa.String(36), nullable=True),
        sa.Column("student_id", sa.String(36), nullable=True),
        sa.Column("task", sa.String(24), nullable=False),
        sa.Column("model", sa.String(80), nullable=False),
        sa.Column("prompt_tokens", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("completion_tokens", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("audio_seconds", sa.Float(), nullable=False, server_default="0"),
        sa.Column("cost_usd", sa.Float(), nullable=False, server_default="0"),
        sa.Column("latency_ms", sa.Integer(), nullable=True),
        sa.Column("ok", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("error", sa.String(200), nullable=True),
        sa.Column("created_at", TS, nullable=True),
    )
    op.create_index("ix_llm_usage_session", "llm_usage", ["session_id"])
    op.create_index("ix_llm_usage_task_created", "llm_usage", ["task", "created_at"])

    op.create_table(
        "job_queue",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("kind", sa.String(40), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.Column("status", sa.String(16), nullable=False, server_default="queued"),
        sa.Column("attempts", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("max_attempts", sa.Integer(), nullable=False, server_default="5"),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column("run_after", TS, nullable=False),
        sa.Column("locked_at", TS, nullable=True),
        sa.Column("locked_by", sa.String(64), nullable=True),
        sa.Column("dedupe_key", sa.String(120), nullable=True, unique=True),
        sa.Column("created_at", TS, nullable=True),
        sa.Column("finished_at", TS, nullable=True),
        sa.CheckConstraint(
            "status in ('queued','running','done','failed','dead')",
            name="ck_job_queue_status",
        ),
    )
    op.create_index("ix_job_queue_claim", "job_queue", ["status", "run_after"])

    # ── Identity ──────────────────────────────────────────────────────────────
    op.create_table(
        "verification_codes",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("purpose", sa.String(20), nullable=False),
        sa.Column("subject", sa.String(200), nullable=False),
        sa.Column("code_hash", sa.String(64), nullable=False),
        sa.Column("student_id", sa.String(36), nullable=True),
        sa.Column("payload", sa.JSON(), nullable=True),
        sa.Column("attempts", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("max_attempts", sa.Integer(), nullable=False, server_default="5"),
        sa.Column("expires_at", TS, nullable=False),
        sa.Column("consumed_at", TS, nullable=True),
        sa.Column("request_ip", sa.String(64), nullable=True),
        sa.Column("created_at", TS, nullable=True),
        sa.CheckConstraint(
            "purpose in ('email_otp','phone_otp','magic_link','oauth_handoff',"
            "'admin_invite')",
            name="ck_verification_codes_purpose",
        ),
    )
    op.create_index("ix_verification_codes_subject", "verification_codes", ["subject"])
    op.create_index("ix_verification_codes_hash", "verification_codes", ["code_hash"])
    op.create_index(
        "ix_verification_codes_lookup",
        "verification_codes",
        ["purpose", "subject", "consumed_at"],
    )

    op.create_table(
        "roster_entries",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column(
            "college_id", sa.String(36), sa.ForeignKey("colleges.id"), nullable=False
        ),
        sa.Column("enrollment_no", sa.String(60), nullable=True),
        sa.Column("name", sa.String(200), nullable=True),
        sa.Column("batch", sa.String(40), nullable=True),
        sa.Column("branch", sa.String(100), nullable=True),
        sa.Column("email", sa.String(200), nullable=True),
        sa.Column("phone", sa.String(20), nullable=True),
        sa.Column("valid_until", TS, nullable=True),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("claimed_by", sa.String(36), nullable=True),
        sa.Column("claimed_at", TS, nullable=True),
        sa.Column("uploaded_by", sa.String(36), nullable=True),
        sa.Column("created_at", TS, nullable=True),
        sa.UniqueConstraint(
            "college_id", "enrollment_no", name="uq_roster_college_enrollment"
        ),
    )
    op.create_index("ix_roster_college_email", "roster_entries", ["college_id", "email"])
    op.create_index("ix_roster_college_phone", "roster_entries", ["college_id", "phone"])

    op.create_table(
        "join_codes",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column(
            "college_id", sa.String(36), sa.ForeignKey("colleges.id"), nullable=False
        ),
        sa.Column("code", sa.String(32), nullable=False, unique=True),
        sa.Column("requires", sa.String(20), nullable=False, server_default="roster_match"),
        sa.Column("max_uses", sa.Integer(), nullable=True),
        sa.Column("uses", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("expires_at", TS, nullable=True),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("created_by", sa.String(36), nullable=True),
        sa.Column("created_at", TS, nullable=True),
        sa.CheckConstraint(
            "requires in ('roster_match','admin_approval')", name="ck_join_codes_requires"
        ),
    )

    op.create_table(
        "enrollment_requests",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column(
            "college_id", sa.String(36), sa.ForeignKey("colleges.id"), nullable=False
        ),
        sa.Column("student_id", sa.String(36), nullable=False),
        sa.Column("method", sa.String(20), nullable=False),
        sa.Column("evidence", sa.JSON(), nullable=True),
        sa.Column("status", sa.String(16), nullable=False, server_default="pending"),
        sa.Column("decided_by", sa.String(36), nullable=True),
        sa.Column("decided_at", TS, nullable=True),
        sa.Column("note", sa.Text(), nullable=True),
        sa.Column("created_at", TS, nullable=True),
        sa.CheckConstraint(
            "status in ('pending','approved','rejected')",
            name="ck_enrollment_requests_status",
        ),
        sa.UniqueConstraint(
            "college_id", "student_id", name="uq_enrollment_requests_pair"
        ),
    )

    op.create_table(
        "consents",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("student_id", sa.String(36), nullable=False),
        sa.Column("kind", sa.String(30), nullable=False),
        sa.Column("version", sa.String(20), nullable=False),
        sa.Column("granted_at", TS, nullable=False),
        sa.Column("revoked_at", TS, nullable=True),
        sa.Column("ip", sa.String(64), nullable=True),
        sa.Column("user_agent", sa.String(400), nullable=True),
        sa.CheckConstraint(
            "kind in ('proctoring','profile_processing','college_sharing')",
            name="ck_consents_kind",
        ),
    )
    op.create_index("ix_consents_student_kind", "consents", ["student_id", "kind", "revoked_at"])

    op.create_table(
        "api_audit_log",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("actor_id", sa.String(36), nullable=True),
        sa.Column("actor_role", sa.String(20), nullable=True),
        sa.Column("action", sa.String(80), nullable=False),
        sa.Column("target", sa.String(120), nullable=True),
        sa.Column("detail", sa.JSON(), nullable=True),
        sa.Column("ip", sa.String(64), nullable=True),
        sa.Column("request_id", sa.String(64), nullable=True),
        sa.Column("created_at", TS, nullable=True),
    )
    op.create_index("ix_api_audit_action", "api_audit_log", ["action"])
    op.create_index("ix_api_audit_target", "api_audit_log", ["target"])
    op.create_index("ix_api_audit_actor_created", "api_audit_log", ["actor_id", "created_at"])

    # ── Proctoring ────────────────────────────────────────────────────────────
    op.create_table(
        "proctoring_events",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("session_id", sa.String(36), nullable=False),
        sa.Column("type", sa.String(40), nullable=False),
        sa.Column("severity", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("detail", sa.JSON(), nullable=True),
        sa.Column("client_ts", TS, nullable=True),
        sa.Column("server_ts", TS, nullable=False),
        sa.Column("snapshot_path", sa.String(300), nullable=True),
        sa.Column("purged_at", TS, nullable=True),
        sa.CheckConstraint("severity between 1 and 5", name="ck_proctoring_severity"),
    )
    op.create_index(
        "ix_proctoring_events_session_ts", "proctoring_events", ["session_id", "server_ts"]
    )
    op.create_index("ix_proctoring_events_type", "proctoring_events", ["type"])

    op.create_table(
        "proctoring_summaries",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("session_id", sa.String(36), nullable=False, unique=True),
        sa.Column("integrity_score", sa.Float(), nullable=True),
        sa.Column("verdict", sa.String(30), nullable=True),
        sa.Column("timeline", sa.JSON(), nullable=True),
        sa.Column(
            "auto_terminated", sa.Boolean(), nullable=False, server_default=sa.false()
        ),
        sa.Column("computed_at", TS, nullable=True),
    )


def downgrade() -> None:
    for table in (
        "proctoring_summaries",
        "proctoring_events",
        "api_audit_log",
        "consents",
        "enrollment_requests",
        "join_codes",
        "roster_entries",
        "verification_codes",
        "job_queue",
        "llm_usage",
        "idempotency_keys",
        "payments",
        "plans",
        "allowance_overrides",
        "allowance_members",
        "allowance_groups",
        "token_ledger",
        "token_lots",
        "token_wallets",
    ):
        op.drop_table(table)

    op.execute("UPDATE students SET role = 'officer' WHERE role = 'college_admin'")
    op.execute("UPDATE students SET role = 'super_admin' WHERE role = 'admin'")

    op.drop_index("ix_students_college_id", table_name="students")
    op.drop_constraint("uq_students_phone", "students", type_="unique")
    for column in (
        "updated_at",
        "enrolled_via",
        "mfa_enabled",
        "mfa_secret",
        "deleted_at",
        "is_active",
        "token_version",
        "password_algo",
        "phone_verified_at",
        "email_verified_at",
        "phone",
    ):
        op.drop_column("students", column)

    for column in (
        "updated_at",
        "deleted_at",
        "snapshot_sharing_allowed",
        "distribution_frozen",
        "daily_interview_cap",
        "monthly_allowance_default",
        "domain_verified_at",
    ):
        op.drop_column("colleges", column)

    op.drop_index("ix_interview_sessions_jd", table_name="interview_sessions")
    op.drop_index(
        "ix_interview_sessions_student_started", table_name="interview_sessions"
    )
    for column in (
        "answered_count",
        "plan_version",
        "ended_reason",
        "integrity_score",
        "proctored",
        "is_custom_jd",
    ):
        op.drop_column("interview_sessions", column)

    op.drop_index("uq_session_transcripts_turn_id", table_name="session_transcripts")
    op.drop_index("ix_session_transcripts_session_turn", table_name="session_transcripts")
    op.drop_index("ix_jd_chunks_jd_id", table_name="jd_chunks")
    op.drop_index("ix_credit_transactions_student", table_name="credit_transactions")
    op.drop_column("session_transcripts", "transcript_source")
    op.drop_column("session_transcripts", "turn_id")

    for column in (
        "scoring_version",
        "integrity_score",
        "weighted_total",
        "competency_scores",
    ):
        op.drop_column("session_scores", column)
    op.drop_column("job_descriptions", "requirements_extracted_at")

    op.execute(
        "ALTER TABLE student_credits ALTER COLUMN updated_at TYPE TEXT "
        "USING updated_at::text"
    )

"""Baseline: the schema as it existed before Phase 1.

Revision ID: 0001_baseline
Revises:
Create Date: 2026-11-27

The schema was created by `create_tables()` on startup plus two ad-hoc scripts, so the
Supabase migration history was empty. This revision records the state those produced.

**On the existing Supabase project, stamp it — do not run it:**

    alembic stamp 0001_baseline

The `CREATE TABLE IF NOT EXISTS` statements exist so that a fresh database (a new
environment, or a staging copy) can be built from migrations alone. Every one is a no-op
against the current production schema.
"""

from __future__ import annotations

from alembic import op

revision: str = "0001_baseline"
down_revision: str | None = None
branch_labels = None
depends_on = None

# Written as raw SQL rather than `op.create_table` so it reproduces the pre-existing
# column types exactly, including the ones the ad-hoc scripts created as TEXT.
_TABLES = """
CREATE TABLE IF NOT EXISTS colleges (
    id VARCHAR(36) PRIMARY KEY,
    name VARCHAR(200) NOT NULL,
    domain VARCHAR(100) UNIQUE,
    credit_limit INTEGER DEFAULT 5,
    is_active BOOLEAN DEFAULT TRUE,
    created_at TIMESTAMP,
    plan INTEGER DEFAULT 100,
    total_credits INTEGER DEFAULT 500,
    credits_used INTEGER DEFAULT 0,
    handler_email VARCHAR(200),
    handler_id VARCHAR(36)
);

CREATE TABLE IF NOT EXISTS students (
    id VARCHAR(36) PRIMARY KEY,
    name VARCHAR(200),
    email VARCHAR(200) UNIQUE,
    role VARCHAR(20) DEFAULT 'student',
    cgpa DOUBLE PRECISION DEFAULT 7.0,
    branch VARCHAR(100),
    year_of_passing INTEGER,
    skills JSON,
    resume_text TEXT,
    password_hash VARCHAR(200),
    college_id VARCHAR(36) REFERENCES colleges(id),
    created_at TIMESTAMP
);

CREATE TABLE IF NOT EXISTS job_descriptions (
    id VARCHAR(36) PRIMARY KEY,
    company_name VARCHAR(200),
    role_title VARCHAR(200),
    required_skills JSON,
    cgpa_cutoff DOUBLE PRECISION DEFAULT 6.0,
    jd_text TEXT,
    difficulty VARCHAR(20) DEFAULT 'medium',
    duration_minutes INTEGER DEFAULT 22,
    is_active BOOLEAN DEFAULT TRUE,
    created_at TIMESTAMP
);

CREATE TABLE IF NOT EXISTS jd_chunks (
    id VARCHAR(36) PRIMARY KEY,
    jd_id VARCHAR(36),
    chunk_text TEXT,
    chunk_index INTEGER
);

CREATE TABLE IF NOT EXISTS interview_sessions (
    id VARCHAR(36) PRIMARY KEY,
    student_id VARCHAR(36),
    jd_id VARCHAR(36),
    status VARCHAR(20) DEFAULT 'active',
    started_at TIMESTAMP,
    ended_at TIMESTAMP,
    duration_seconds INTEGER,
    question_count INTEGER DEFAULT 0,
    total_filler_words INTEGER DEFAULT 0,
    avg_wpm DOUBLE PRECISION
);

CREATE TABLE IF NOT EXISTS session_transcripts (
    id VARCHAR(36) PRIMARY KEY,
    session_id VARCHAR(36),
    turn_number INTEGER,
    phase VARCHAR(20),
    question TEXT,
    answer TEXT,
    answer_wpm DOUBLE PRECISION,
    filler_count INTEGER DEFAULT 0,
    created_at TIMESTAMP
);

CREATE TABLE IF NOT EXISTS session_scores (
    id VARCHAR(36) PRIMARY KEY,
    session_id VARCHAR(36) UNIQUE,
    overall_score DOUBLE PRECISION,
    technical_score DOUBLE PRECISION,
    communication_score DOUBLE PRECISION,
    body_language_score DOUBLE PRECISION,
    avg_eye_contact_pct DOUBLE PRECISION,
    avg_posture_score DOUBLE PRECISION,
    verdict VARCHAR(50),
    topics_json JSON,
    strengths_json JSON,
    improvements_json JSON,
    tips_json JSON,
    evaluation_status VARCHAR(20) DEFAULT 'pending',
    evaluated_at TIMESTAMP
);

CREATE TABLE IF NOT EXISTS shortlists (
    id VARCHAR(36) PRIMARY KEY,
    jd_id VARCHAR(36),
    officer_id VARCHAR(36),
    status VARCHAR(20) DEFAULT 'processing',
    results_json JSON,
    created_at TIMESTAMP
);

CREATE TABLE IF NOT EXISTS student_jds (
    id VARCHAR(36) PRIMARY KEY,
    student_id VARCHAR(36) NOT NULL,
    company_name VARCHAR(200) NOT NULL,
    role_title VARCHAR(200) NOT NULL,
    jd_text TEXT NOT NULL,
    match_pct INTEGER DEFAULT 0,
    matched_skills TEXT DEFAULT '[]',
    missing_skills TEXT DEFAULT '[]',
    advice TEXT DEFAULT '',
    status VARCHAR(20) DEFAULT 'pending',
    created_at TIMESTAMP
);
CREATE INDEX IF NOT EXISTS ix_student_jds_student_id ON student_jds (student_id);

-- Created by api/credits.py at request time, hence the TEXT columns.
CREATE TABLE IF NOT EXISTS student_credits (
    id TEXT PRIMARY KEY,
    student_id TEXT UNIQUE NOT NULL,
    credits INTEGER DEFAULT 3,
    total_used INTEGER DEFAULT 0,
    updated_at TEXT
);

CREATE TABLE IF NOT EXISTS credit_transactions (
    id TEXT PRIMARY KEY,
    student_id TEXT NOT NULL,
    college_id TEXT,
    amount INTEGER NOT NULL,
    reason TEXT NOT NULL,
    created_by TEXT,
    created_at TIMESTAMP
);

-- Created by create_profile_table.py.
CREATE TABLE IF NOT EXISTS student_profile_data (
    student_id TEXT PRIMARY KEY,
    scraped_summary TEXT,
    urls_found INTEGER DEFAULT 0,
    updated_at TIMESTAMP
);
"""


def upgrade() -> None:
    for statement in _split(_TABLES):
        op.execute(statement)


def downgrade() -> None:
    # Refusing is deliberate: this revision is the floor, and "downgrade" here would mean
    # dropping the tables holding every interview and transcript on the platform.
    raise RuntimeError(
        "0001_baseline cannot be downgraded — it would drop all production data."
    )


def _split(script: str) -> list[str]:
    statements = []
    for raw in script.split(";"):
        statement = "\n".join(
            line for line in raw.splitlines() if not line.strip().startswith("--")
        ).strip()
        if statement:
            statements.append(statement)
    return statements

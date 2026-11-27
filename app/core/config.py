"""Application settings.

Single source of truth for configuration. Values come from the environment (or a local
`.env` file); nothing security-relevant has a usable default. In `ENV=production` the
validators below refuse to start the app on an unsafe configuration rather than silently
running with one.
"""

from __future__ import annotations

from functools import lru_cache
from typing import Literal

from pydantic import field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

Env = Literal["local", "preview", "production"]

# Values that used to be hardcoded defaults and must never be accepted again.
_FORBIDDEN_SECRETS = {"secret", "changeme", "please-change-me", ""}


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=True,
    )

    # ── Environment ───────────────────────────────────────────────────────────
    ENV: Env = "local"
    LOG_LEVEL: str = "INFO"
    SENTRY_DSN: str = ""

    # ── Database ──────────────────────────────────────────────────────────────
    DATABASE_URL: str = ""
    # Vercel functions are short-lived and numerous; keep per-process pools tiny and let
    # the Supabase pooler do the multiplexing. Overridable per environment.
    DB_POOL_SIZE: int = 2
    DB_MAX_OVERFLOW: int = 3
    DB_POOL_RECYCLE_SEC: int = 300
    DB_STATEMENT_TIMEOUT_MS: int = 15_000

    # ── Redis (Upstash) ───────────────────────────────────────────────────────
    # REST is preferred on serverless; the TCP URL is used for local development.
    UPSTASH_REDIS_REST_URL: str = ""
    UPSTASH_REDIS_REST_TOKEN: str = ""
    REDIS_URL: str = ""
    SESSION_TTL_SEC: int = 7_200

    # ── Auth ──────────────────────────────────────────────────────────────────
    JWT_SECRET: str = ""
    JWT_ALGORITHM: str = "HS256"
    JWT_EXPIRE_HOURS: int = 24
    BCRYPT_ROUNDS: int = 12
    # Bootstrapped once into the DB on first startup, then the DB is authoritative.
    ADMIN_BOOTSTRAP_EMAILS: str = ""

    # ── CORS ──────────────────────────────────────────────────────────────────
    CORS_ORIGINS: str = "http://localhost:8080,http://localhost:5173"
    # Vercel preview deployments, e.g. placeprepai-frontend-git-xyz.vercel.app
    CORS_ORIGIN_REGEX: str = ""

    FRONTEND_URL: str = "http://localhost:8080"

    # ── Groq / LLM ────────────────────────────────────────────────────────────
    GROQ_API_KEY: str = ""
    GROQ_API_KEY2: str = ""
    # Per-task model IDs. llama-3.3-70b-versatile is Enterprise-only on Groq as of
    # 2026-11; these are the self-serve production models.
    LLM_MODEL_QUESTION: str = "openai/gpt-oss-120b"
    LLM_MODEL_GRADER: str = "openai/gpt-oss-120b"
    LLM_MODEL_EXTRACTOR: str = "openai/gpt-oss-120b"
    LLM_MODEL_EVALUATOR: str = "openai/gpt-oss-120b"
    LLM_MODEL_CHEAP: str = "openai/gpt-oss-20b"
    LLM_MODEL_FALLBACK: str = ""
    STT_MODEL: str = "whisper-large-v3-turbo"

    LLM_TIMEOUT_SEC: float = 20.0
    LLM_MAX_RETRIES: int = 2
    LLM_MAX_CONCURRENCY: int = 8
    LLM_TOKEN_BUDGET_QUESTION: int = 200
    LLM_TOKEN_BUDGET_GRADER: int = 600
    LLM_TOKEN_BUDGET_EVALUATOR: int = 1_200

    # ── Google OAuth ──────────────────────────────────────────────────────────
    GOOGLE_CLIENT_ID: str = ""
    GOOGLE_CLIENT_SECRET: str = ""
    GOOGLE_REDIRECT_URI: str = "http://localhost:8000/api/auth/google/callback"
    OAUTH_CODE_TTL_SEC: int = 120

    # ── Razorpay ──────────────────────────────────────────────────────────────
    RAZORPAY_KEY_ID: str = ""
    RAZORPAY_KEY_SECRET: str = ""
    RAZORPAY_WEBHOOK_SECRET: str = ""

    # ── Supabase ──────────────────────────────────────────────────────────────
    SUPABASE_URL: str = ""
    SUPABASE_SERVICE_ROLE_KEY: str = ""
    SUPABASE_RESUME_BUCKET: str = "resumes"
    SUPABASE_SNAPSHOT_BUCKET: str = "proctoring-snapshots"
    SNAPSHOT_RETENTION_DAYS: int = 14

    # ── External context ──────────────────────────────────────────────────────
    GITHUB_TOKEN: str = ""
    GITHUB_CACHE_TTL_SEC: int = 86_400
    LEETCODE_CACHE_TTL_SEC: int = 86_400
    SCRAPE_TIMEOUT_SEC: float = 8.0
    SCRAPE_MAX_BYTES: int = 2_000_000

    # ── Notifications (OTP delivery) ──────────────────────────────────────────
    # Email goes through Resend. Locally, with no key set, codes go to the log.
    EMAIL_FROM: str = "PlacePrep AI <no-reply@placeprepai.fun>"
    RESEND_API_KEY: str = ""
    # SMS goes through MSG91. Kept behind a flag that stays false until the DLT
    # template is approved: sending before then is rejected by the carrier, and a
    # silent rejection looks exactly like a delivered message from our side.
    SMS_ENABLED: bool = False
    SMS_API_KEY: str = ""
    MSG91_TEMPLATE_ID: str = ""
    MSG91_SENDER_ID: str = ""
    OTP_TTL_SEC: int = 600
    OTP_MAX_ATTEMPTS: int = 5

    # ── Consent (India DPDP Act) ──────────────────────────────────────────────
    # Bump a version when the notice text changes: an old consent then stops counting
    # as current, and the student is asked again rather than silently carried over.
    CONSENT_VERSION_PROCTORING: str = "2026-11-27"
    CONSENT_VERSION_PROFILE: str = "2026-11-27"
    CONSENT_VERSION_COLLEGE_SHARING: str = "2026-11-27"
    # A proctored interview cannot start without a current proctoring consent.
    REQUIRE_PROCTORING_CONSENT: bool = True

    # ── Scheduled jobs ────────────────────────────────────────────────────────
    # Vercel Cron sends this as a bearer token. Without it the cron endpoints
    # refuse to run rather than being callable by anyone.
    CRON_SECRET: str = ""

    # ── Uploads ───────────────────────────────────────────────────────────────
    MAX_UPLOAD_BYTES: int = 5_242_880  # 5 MB

    # ── Interview engine ──────────────────────────────────────────────────────
    MAX_QUESTIONS: int = 12
    MIN_QUESTIONS: int = 7
    MAX_MINUTES: int = 22
    HARD_CUTOFF_MIN: int = 25
    WARMUP_QUESTIONS: int = 2
    FEEDBACK_EVERY_N: int = 3

    # ── Tokens / quota ────────────────────────────────────────────────────────
    TOKEN_CONSUME_AFTER_ANSWERS: int = 3
    TOKEN_CONSUME_AFTER_SEC: int = 300
    MAX_AUTO_RELEASES_PER_DAY: int = 2
    DEFAULT_COLLEGE_MONTHLY_ALLOWANCE: int = 4
    DEFAULT_DAILY_INTERVIEW_CAP: int = 3
    FREE_TIER_TOKENS: int = 2
    COLLEGE_POOL_VALIDITY_DAYS: int = 365

    # ── Rate limits (requests per window, seconds) ────────────────────────────
    RATE_LIMIT_AUTH: str = "10/60"
    RATE_LIMIT_OTP: str = "5/600"
    RATE_LIMIT_UPLOAD: str = "5/300"
    RATE_LIMIT_INTERVIEW_START: str = "5/600"
    RATE_LIMIT_LLM: str = "30/60"
    RATE_LIMIT_DEFAULT: str = "120/60"

    # ── Proctoring ────────────────────────────────────────────────────────────
    PROCTOR_AUTO_STOP_MULTIFACE_SEC: int = 15
    PROCTOR_AUTO_STOP_NOFACE_SEC: int = 30

    # ── Derived helpers ───────────────────────────────────────────────────────

    @property
    def is_production(self) -> bool:
        return self.ENV == "production"

    @property
    def cors_origins(self) -> list[str]:
        return [o.strip() for o in self.CORS_ORIGINS.split(",") if o.strip()]

    @property
    def admin_bootstrap_emails(self) -> list[str]:
        return [
            e.strip().lower()
            for e in self.ADMIN_BOOTSTRAP_EMAILS.split(",")
            if e.strip()
        ]

    @property
    def sqlalchemy_url(self) -> str:
        """DATABASE_URL normalised for SQLAlchemy 2.x."""
        url = self.DATABASE_URL
        if url.startswith("postgres://"):
            url = url.replace("postgres://", "postgresql://", 1)
        return url

    @property
    def redis_configured(self) -> bool:
        return bool(
            (self.UPSTASH_REDIS_REST_URL and self.UPSTASH_REDIS_REST_TOKEN)
            or self.REDIS_URL
        )

    # ── Validators ────────────────────────────────────────────────────────────

    @field_validator("CORS_ORIGINS")
    @classmethod
    def _no_wildcard_origin(cls, v: str) -> str:
        if "*" in v:
            raise ValueError(
                "CORS_ORIGINS must not contain '*'. Credentialed requests with a "
                "wildcard origin are unsafe; list the exact origins."
            )
        return v

    @model_validator(mode="after")
    def _validate_for_environment(self) -> Settings:
        problems: list[str] = []

        if self.JWT_SECRET.strip().lower() in _FORBIDDEN_SECRETS:
            problems.append("JWT_SECRET is unset or a known placeholder value")
        elif self.is_production and len(self.JWT_SECRET) < 32:
            problems.append("JWT_SECRET must be at least 32 characters in production")

        if not self.DATABASE_URL:
            problems.append("DATABASE_URL is required")
        elif self.is_production and self.DATABASE_URL.startswith("sqlite"):
            problems.append("DATABASE_URL must not be SQLite in production")

        if self.is_production:
            if not self.redis_configured:
                problems.append(
                    "Redis is required in production "
                    "(UPSTASH_REDIS_REST_URL + _TOKEN, or REDIS_URL)"
                )
            if not self.GROQ_API_KEY:
                problems.append("GROQ_API_KEY is required")
            if not self.admin_bootstrap_emails:
                problems.append("ADMIN_BOOTSTRAP_EMAILS is required")
            if self.RAZORPAY_KEY_ID and not self.RAZORPAY_WEBHOOK_SECRET:
                problems.append(
                    "RAZORPAY_WEBHOOK_SECRET is required when Razorpay is enabled "
                    "(tokens are granted only from the verified webhook)"
                )
            if self.RAZORPAY_KEY_ID.startswith("rzp_test_"):
                problems.append("RAZORPAY_KEY_ID is a test key but ENV=production")
            if not self.SUPABASE_SERVICE_ROLE_KEY:
                problems.append(
                    "SUPABASE_SERVICE_ROLE_KEY is required (private Storage buckets)"
                )
            for origin in self.cors_origins:
                if origin.startswith("http://"):
                    problems.append(f"CORS origin {origin!r} is not HTTPS")

        if problems:
            bullets = "\n  - ".join(problems)
            raise ValueError(
                f"Invalid configuration for ENV={self.ENV}:\n  - {bullets}"
            )
        return self


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()


settings = get_settings()

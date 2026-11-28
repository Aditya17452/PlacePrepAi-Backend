"""Plans, payments, idempotency and LLM cost accounting (PLAN.md §8.5, §9, §10).

The `plans` table is the only source of truth for prices. The client sends a plan code and
never a price or a token count, so a tampered request cannot buy 50 interviews for Rs 149.

Tokens are granted **only** from the verified `payment.captured` webhook. `payments.
payment_id` is unique, which is what makes the grant idempotent against webhook retries
and against a replayed client-side verify call.
"""

from __future__ import annotations

from sqlalchemy import (
    JSON,
    Boolean,
    CheckConstraint,
    Column,
    DateTime,
    Float,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)

from app.models.base import Base, gen_uuid, utcnow

PAYMENT_STATUSES = ("created", "captured", "failed", "refunded", "partially_refunded")


class Plan(Base):
    """A purchasable bundle. Prices are stored in paise, inclusive of GST."""

    __tablename__ = "plans"

    code = Column(String(40), primary_key=True)
    #: 'student' (direct, self-serve) | 'college' (invoiced, admin-granted)
    audience = Column(String(10), nullable=False, default="student")
    name = Column(String(120), nullable=False)
    price_paise = Column(Integer, nullable=False)
    tokens = Column(Integer, nullable=False)
    validity_days = Column(Integer, nullable=True)  # NULL = lifetime
    is_active = Column(Boolean, default=True, nullable=False)
    is_recommended = Column(Boolean, default=False, nullable=False)
    #: Free plans require verified phone + email before they can be claimed.
    requires_phone_verification = Column(Boolean, default=False, nullable=False)
    #: One claim per verified phone, ever (the free tier).
    once_per_phone = Column(Boolean, default=False, nullable=False)
    sort_order = Column(Integer, default=0, nullable=False)
    notes = Column(Text, nullable=True)
    created_at = Column(DateTime, default=utcnow)
    updated_at = Column(DateTime, default=utcnow, onupdate=utcnow)

    __table_args__ = (
        CheckConstraint("price_paise >= 0", name="ck_plans_price_non_negative"),
        CheckConstraint("tokens >= 0", name="ck_plans_tokens_non_negative"),
        CheckConstraint(
            "audience in ('student','college')", name="ck_plans_audience"
        ),
    )

    @property
    def price_rupees(self) -> float:
        return self.price_paise / 100


class Payment(Base):
    """One Razorpay order and its outcome.

    `payment_id` is NULL until capture, and unique once set — that uniqueness is the
    idempotency guarantee for token granting.
    """

    __tablename__ = "payments"

    id = Column(String(36), primary_key=True, default=gen_uuid)
    student_id = Column(String(36), nullable=False, index=True)
    plan_code = Column(String(40), nullable=False)
    order_id = Column(String(64), nullable=False, unique=True)
    payment_id = Column(String(64), nullable=True, unique=True)
    amount_paise = Column(Integer, nullable=False)
    currency = Column(String(8), nullable=False, default="INR")
    status = Column(String(24), nullable=False, default="created")
    tokens_granted = Column(Integer, nullable=False, default=0)
    #: True only after the webhook signature verified. Nothing is granted without it.
    signature_verified = Column(Boolean, default=False, nullable=False)
    raw_webhook = Column(JSON, nullable=True)
    refunded_paise = Column(Integer, nullable=False, default=0)
    invoice_no = Column(String(40), nullable=True, unique=True)
    gst_paise = Column(Integer, nullable=True)
    created_at = Column(DateTime, default=utcnow, index=True)
    captured_at = Column(DateTime, nullable=True)
    updated_at = Column(DateTime, default=utcnow, onupdate=utcnow)

    __table_args__ = (
        CheckConstraint(
            "status in ('created','captured','failed','refunded','partially_refunded')",
            name="ck_payments_status",
        ),
        CheckConstraint("amount_paise >= 0", name="ck_payments_amount_non_negative"),
        Index("ix_payments_student_created", "student_id", "created_at"),
    )


class IdempotencyKey(Base):
    """Replay protection for mutating endpoints that a client may retry."""

    __tablename__ = "idempotency_keys"

    key = Column(String(128), primary_key=True)
    scope = Column(String(60), nullable=False)
    actor_id = Column(String(36), nullable=True)
    response = Column(JSON, nullable=True)
    created_at = Column(DateTime, default=utcnow, index=True)


class LlmUsage(Base):
    """Per-call token and audio accounting, so cost per interview is measured.

    Feeds the §9 guardrail: do not launch the Rs 10 / Rs 9 college tiers until the
    measured cost per interview is under Rs 5.
    """

    __tablename__ = "llm_usage"

    id = Column(String(36), primary_key=True, default=gen_uuid)
    session_id = Column(String(36), nullable=True, index=True)
    student_id = Column(String(36), nullable=True, index=True)
    #: 'question' | 'grader' | 'extractor' | 'evaluator' | 'cheap' | 'stt'
    task = Column(String(24), nullable=False)
    model = Column(String(80), nullable=False)
    prompt_tokens = Column(Integer, default=0, nullable=False)
    completion_tokens = Column(Integer, default=0, nullable=False)
    audio_seconds = Column(Float, default=0.0, nullable=False)
    #: Computed at write time from a price table, so historical rows keep their cost.
    cost_usd = Column(Float, default=0.0, nullable=False)
    latency_ms = Column(Integer, nullable=True)
    ok = Column(Boolean, default=True, nullable=False)
    error = Column(String(200), nullable=True)
    created_at = Column(DateTime, default=utcnow, index=True)

    __table_args__ = (
        Index("ix_llm_usage_task_created", "task", "created_at"),
    )


class JobQueue(Base):
    """Durable background work.

    Replaces `asyncio.create_task`, which silently loses evaluation and JD-matching work
    whenever the process restarts — and on Vercel the process always goes away. Drained by
    a cron-triggered worker endpoint.
    """

    __tablename__ = "job_queue"

    id = Column(String(36), primary_key=True, default=gen_uuid)
    #: 'evaluate_session' | 'match_student_jd' | 'build_candidate_context' |
    #: 'expire_token_lots' | 'purge_snapshots'
    kind = Column(String(40), nullable=False)
    payload = Column(JSON, nullable=False, default=dict)
    status = Column(String(16), nullable=False, default="queued")
    attempts = Column(Integer, default=0, nullable=False)
    max_attempts = Column(Integer, default=5, nullable=False)
    last_error = Column(Text, nullable=True)
    run_after = Column(DateTime, default=utcnow, nullable=False)
    locked_at = Column(DateTime, nullable=True)
    locked_by = Column(String(64), nullable=True)
    #: Set for jobs that must not be enqueued twice (e.g. one evaluation per session).
    dedupe_key = Column(String(120), nullable=True)
    created_at = Column(DateTime, default=utcnow)
    finished_at = Column(DateTime, nullable=True)

    __table_args__ = (
        CheckConstraint(
            "status in ('queued','running','done','failed','dead')",
            name="ck_job_queue_status",
        ),
        UniqueConstraint("dedupe_key", name="uq_job_queue_dedupe"),
        Index("ix_job_queue_claim", "status", "run_after"),
    )

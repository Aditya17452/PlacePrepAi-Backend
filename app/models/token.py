"""Token wallets, lots and ledger. One token = one interview (PLAN.md §8).

Design notes that matter when reading the service layer:

* `token_ledger` is **append only**. Nothing updates or deletes a ledger row.
* `token_wallets.balance` is a cache of the ledger sum, written in the same transaction as
  the ledger row under `SELECT ... FOR UPDATE`, with a check constraint that it can never
  go negative.
* `token_lots` carry expiry so consumption is FIFO by earliest expiry.
* Allowances are **limits, not reservations**. An allowance never moves tokens; it only
  caps how much of the college pool one student may draw. The sum of allowances across a
  college is allowed to exceed the pool.
"""

from __future__ import annotations

from sqlalchemy import (
    JSON,
    Boolean,
    CheckConstraint,
    Column,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    text,
)

from app.models.base import Base, gen_uuid, utcnow

# Ledger entry kinds. Positive amounts add spendable tokens, negative remove them;
# `reserve` holds them without consuming, `release` returns a reservation.
LEDGER_KINDS = (
    "grant",  # admin created tokens in a college pool
    "purchase",  # student bought tokens (webhook-verified)
    "reserve",  # held for an in-progress interview
    "release",  # reservation returned (our fault, or below the consume threshold)
    "consume",  # interview passed the consume threshold
    "refund",  # payment refunded, unused tokens revoked
    "expire",  # lot expiry swept
    "revoke",  # admin removed tokens, with a reason
    "adjust",  # manual correction, with a reason
)

OWNER_TYPES = ("student", "college")


class TokenWallet(Base):
    """One wallet per student (personal) and one per college (the pool)."""

    __tablename__ = "token_wallets"

    id = Column(String(36), primary_key=True, default=gen_uuid)
    owner_type = Column(String(10), nullable=False)
    owner_id = Column(String(36), nullable=False)
    #: Cached sum of the ledger. Never trust it without the ledger; never let it go < 0.
    balance = Column(Integer, nullable=False, default=0)
    #: Currently reserved for in-progress interviews (subset of balance).
    reserved = Column(Integer, nullable=False, default=0)
    created_at = Column(DateTime, default=utcnow)
    updated_at = Column(DateTime, default=utcnow, onupdate=utcnow)

    __table_args__ = (
        UniqueConstraint("owner_type", "owner_id", name="uq_token_wallets_owner"),
        CheckConstraint("balance >= 0", name="ck_token_wallets_balance_non_negative"),
        CheckConstraint("reserved >= 0", name="ck_token_wallets_reserved_non_negative"),
        CheckConstraint(
            "owner_type in ('student','college')", name="ck_token_wallets_owner_type"
        ),
    )


class TokenLot(Base):
    """A batch of tokens with its own expiry, consumed FIFO by earliest expiry."""

    __tablename__ = "token_lots"

    id = Column(String(36), primary_key=True, default=gen_uuid)
    wallet_id = Column(
        String(36), ForeignKey("token_wallets.id"), nullable=False, index=True
    )
    #: 'grant' (college pool) | 'purchase' (student plan) | 'free' (free tier)
    source = Column(String(20), nullable=False)
    qty = Column(Integer, nullable=False)
    remaining = Column(Integer, nullable=False)
    expires_at = Column(DateTime, nullable=True)
    #: Set for purchases, so a refund can revoke exactly this lot's unused tokens.
    payment_id = Column(String(64), nullable=True, index=True)
    plan_code = Column(String(40), nullable=True)
    expiry_notified_at = Column(DateTime, nullable=True)
    created_at = Column(DateTime, default=utcnow)

    __table_args__ = (
        CheckConstraint("remaining >= 0", name="ck_token_lots_remaining_non_negative"),
        CheckConstraint("remaining <= qty", name="ck_token_lots_remaining_lte_qty"),
        Index("ix_token_lots_wallet_expiry", "wallet_id", "expires_at"),
    )


class TokenLedger(Base):
    """Append-only record of every token movement."""

    __tablename__ = "token_ledger"

    id = Column(String(36), primary_key=True, default=gen_uuid)
    wallet_id = Column(
        String(36), ForeignKey("token_wallets.id"), nullable=False, index=True
    )
    lot_id = Column(String(36), ForeignKey("token_lots.id"), nullable=True)
    kind = Column(String(20), nullable=False)
    #: Signed. `reserve` is recorded as 0 against balance and tracked in `reserved`.
    amount = Column(Integer, nullable=False)
    reason = Column(Text, nullable=False)
    #: Who caused this. NULL for system sweeps (expiry).
    actor_id = Column(String(36), nullable=True)
    session_id = Column(String(36), nullable=True, index=True)
    payment_id = Column(String(64), nullable=True, index=True)
    #: Which wallet funded a student's interview, when it came from a college pool.
    on_behalf_of = Column(String(36), nullable=True)
    created_at = Column(DateTime, default=utcnow, index=True)

    __table_args__ = (
        CheckConstraint(
            "kind in ('grant','purchase','reserve','release','consume','refund',"
            "'expire','revoke','adjust')",
            name="ck_token_ledger_kind",
        ),
        # One reservation per session, so a retried interview start cannot double-reserve.
        Index(
            "uq_token_ledger_session_reserve",
            "session_id",
            unique=True,
            postgresql_where=text("kind = 'reserve'"),
            sqlite_where=text("kind = 'reserve'"),
        ),
    )


class AllowanceGroup(Base):
    """A cap on how much of the college pool each member student may draw."""

    __tablename__ = "allowance_groups"

    id = Column(String(36), primary_key=True, default=gen_uuid)
    college_id = Column(
        String(36), ForeignKey("colleges.id"), nullable=False, index=True
    )
    name = Column(String(200), nullable=False)
    #: Roster filters this group was built from, kept so it can be refreshed.
    filters = Column(JSON, default=dict)
    per_student_allowance = Column(Integer, nullable=False)
    #: 'month' (rolling calendar month) | 'term' (whole validity window)
    period = Column(String(10), nullable=False, default="month")
    valid_from = Column(DateTime, nullable=True)
    valid_until = Column(DateTime, nullable=True)
    is_default = Column(Boolean, default=False, nullable=False)
    created_by = Column(String(36), nullable=True)
    created_at = Column(DateTime, default=utcnow)

    __table_args__ = (
        CheckConstraint(
            "per_student_allowance >= 0", name="ck_allowance_groups_non_negative"
        ),
        CheckConstraint("period in ('month','term')", name="ck_allowance_groups_period"),
    )


class AllowanceMember(Base):
    __tablename__ = "allowance_members"

    id = Column(String(36), primary_key=True, default=gen_uuid)
    group_id = Column(
        String(36), ForeignKey("allowance_groups.id"), nullable=False, index=True
    )
    student_id = Column(String(36), nullable=False, index=True)
    created_at = Column(DateTime, default=utcnow)

    __table_args__ = (
        UniqueConstraint("group_id", "student_id", name="uq_allowance_members"),
    )


class AllowanceOverride(Base):
    """Extra allowance for one student: an individual top-up, or a time-boxed drive."""

    __tablename__ = "allowance_overrides"

    id = Column(String(36), primary_key=True, default=gen_uuid)
    college_id = Column(
        String(36), ForeignKey("colleges.id"), nullable=False, index=True
    )
    student_id = Column(String(36), nullable=False, index=True)
    #: 'topup' | 'drive'
    kind = Column(String(10), nullable=False, default="topup")
    extra_allowance = Column(Integer, nullable=False)
    label = Column(String(200), nullable=True)
    reason = Column(Text, nullable=False)
    #: A drive may pin a custom JD for its interviews.
    jd_id = Column(String(36), nullable=True)
    valid_from = Column(DateTime, nullable=True)
    valid_until = Column(DateTime, nullable=True)
    created_by = Column(String(36), nullable=True)
    created_at = Column(DateTime, default=utcnow)

    __table_args__ = (
        CheckConstraint(
            "extra_allowance >= 0", name="ck_allowance_overrides_non_negative"
        ),
        CheckConstraint("kind in ('topup','drive')", name="ck_allowance_overrides_kind"),
    )

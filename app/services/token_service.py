"""Token accounting: one token = one interview (PLAN.md §8).

This module is the only place token balances change. Everything it does happens inside the
caller's transaction, under a row lock on the wallet, and always writes a ledger row —
so a balance can be re-derived from the ledger if the cached figure is ever doubted.

The three rules worth stating plainly:

1. **Reserve at start, consume at the threshold.** An interview that dies in the first
   couple of questions because *our* LLM provider was down does not cost the student a
   token.
2. **Allowances are limits, not reservations.** A college allowance caps how much of the
   pool one student may draw; it never moves tokens, and the sum of allowances across a
   college may exceed the pool.
3. **Never trust the client.** No caller passes a token count or a wallet; this module
   derives both from the authenticated student and the database.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Literal

from sqlalchemy import func
from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.logging_setup import get_logger
from app.models import (
    AllowanceGroup,
    AllowanceMember,
    AllowanceOverride,
    College,
    InterviewSession,
    Student,
    TokenLedger,
    TokenLot,
    TokenWallet,
    utcnow,
)

log = get_logger("tokens")

OwnerType = Literal["student", "college"]


class TokenError(RuntimeError):
    """Base class for refusals that should reach the user as a 4xx."""


class InsufficientTokens(TokenError):
    pass


class AllowanceExhausted(TokenError):
    pass


class DailyCapReached(TokenError):
    pass


class InterviewAlreadyActive(TokenError):
    pass


class CollegeInactive(TokenError):
    pass


# ── Wallets ───────────────────────────────────────────────────────────────────


def get_or_create_wallet(
    db: Session, owner_type: OwnerType, owner_id: str, *, lock: bool = False
) -> TokenWallet:
    query = db.query(TokenWallet).filter(
        TokenWallet.owner_type == owner_type,
        TokenWallet.owner_id == owner_id,
    )
    if lock:
        # No-op on SQLite; on Postgres this serialises concurrent spends on one wallet.
        query = query.with_for_update()
    wallet = query.first()
    if wallet is not None:
        return wallet

    wallet = TokenWallet(owner_type=owner_type, owner_id=owner_id, balance=0, reserved=0)
    db.add(wallet)
    db.flush()
    return wallet


def _append(
    db: Session,
    wallet: TokenWallet,
    *,
    kind: str,
    amount: int,
    reason: str,
    actor_id: str | None = None,
    session_id: str | None = None,
    payment_id: str | None = None,
    lot_id: str | None = None,
    on_behalf_of: str | None = None,
) -> TokenLedger:
    entry = TokenLedger(
        wallet_id=wallet.id,
        lot_id=lot_id,
        kind=kind,
        amount=amount,
        reason=reason,
        actor_id=actor_id,
        session_id=session_id,
        payment_id=payment_id,
        on_behalf_of=on_behalf_of,
    )
    db.add(entry)
    return entry


# ── Creating tokens ───────────────────────────────────────────────────────────


def grant(
    db: Session,
    *,
    owner_type: OwnerType,
    owner_id: str,
    qty: int,
    reason: str,
    actor_id: str | None,
    validity_days: int | None = None,
    source: str = "grant",
    payment_id: str | None = None,
    plan_code: str | None = None,
) -> TokenLot:
    """Create `qty` tokens in a wallet as a new lot. Caller commits."""
    if qty <= 0:
        raise TokenError("Grant quantity must be positive")
    if not reason.strip():
        raise TokenError("A reason is required for every grant")

    wallet = get_or_create_wallet(db, owner_type, owner_id, lock=True)
    expires_at = (
        utcnow() + timedelta(days=validity_days) if validity_days is not None else None
    )

    lot = TokenLot(
        wallet_id=wallet.id,
        source=source,
        qty=qty,
        remaining=qty,
        expires_at=expires_at,
        payment_id=payment_id,
        plan_code=plan_code,
    )
    db.add(lot)
    db.flush()

    wallet.balance = int(wallet.balance) + qty
    _append(
        db,
        wallet,
        kind="purchase" if source == "purchase" else "grant",
        amount=qty,
        reason=reason,
        actor_id=actor_id,
        payment_id=payment_id,
        lot_id=lot.id,
    )
    log.info(
        "tokens_granted",
        owner_type=owner_type,
        owner_id=owner_id,
        qty=qty,
        source=source,
    )
    return lot


def revoke(
    db: Session,
    *,
    owner_type: OwnerType,
    owner_id: str,
    qty: int,
    reason: str,
    actor_id: str | None,
    payment_id: str | None = None,
) -> int:
    """Remove up to `qty` *unused* tokens, newest lots first. Returns how many went.

    Used by admin revocation and by refunds, which may only take back tokens the student
    has not already spent.
    """
    if qty <= 0:
        raise TokenError("Revoke quantity must be positive")

    wallet = get_or_create_wallet(db, owner_type, owner_id, lock=True)
    spendable = int(wallet.balance) - int(wallet.reserved)
    taking = min(qty, max(spendable, 0))
    if taking == 0:
        return 0

    lots_query = db.query(TokenLot).filter(
        TokenLot.wallet_id == wallet.id, TokenLot.remaining > 0
    )
    if payment_id:
        lots_query = lots_query.filter(TokenLot.payment_id == payment_id)
    lots = lots_query.order_by(TokenLot.created_at.desc()).all()

    remaining_to_take = taking
    for lot in lots:
        if remaining_to_take == 0:
            break
        take = min(int(lot.remaining), remaining_to_take)
        lot.remaining = int(lot.remaining) - take
        remaining_to_take -= take
        _append(
            db,
            wallet,
            kind="refund" if payment_id else "revoke",
            amount=-take,
            reason=reason,
            actor_id=actor_id,
            payment_id=payment_id,
            lot_id=lot.id,
        )

    actually_taken = taking - remaining_to_take
    wallet.balance = int(wallet.balance) - actually_taken
    log.info("tokens_revoked", owner_id=owner_id, qty=actually_taken, reason=reason)
    return actually_taken


# ── Allowances ────────────────────────────────────────────────────────────────


def _period_start(period: str, group: AllowanceGroup | None) -> datetime:
    now = utcnow()
    if period == "month":
        return now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    if group is not None and group.valid_from is not None:
        return group.valid_from
    return datetime(1970, 1, 1)


@dataclass(frozen=True)
class AllowanceView:
    allowance: int
    used_this_period: int
    remaining: int
    period: str


def college_allowance(db: Session, student: Student, college: College) -> AllowanceView:
    """How many pool tokens this student may still draw in the current period.

    A student in several groups gets the **most generous** group allowance (not the sum —
    summing overlapping groups surprises people), plus any active individual top-ups and
    drive allowances on top.
    """
    now = utcnow()

    groups = (
        db.query(AllowanceGroup)
        .join(AllowanceMember, AllowanceMember.group_id == AllowanceGroup.id)
        .filter(
            AllowanceGroup.college_id == college.id,
            AllowanceMember.student_id == student.id,
        )
        .all()
    )
    groups = [g for g in groups if _group_active(g, now)]

    best_group: AllowanceGroup | None = None
    base = 0
    for group in groups:
        if int(group.per_student_allowance) > base:
            base = int(group.per_student_allowance)
            best_group = group

    period = best_group.period if best_group else "month"
    if not groups:
        # No explicit group: fall back to the college default (4/month).
        base = int(
            college.monthly_allowance_default
            or settings.DEFAULT_COLLEGE_MONTHLY_ALLOWANCE
        )

    overrides = (
        db.query(func.coalesce(func.sum(AllowanceOverride.extra_allowance), 0))
        .filter(
            AllowanceOverride.college_id == college.id,
            AllowanceOverride.student_id == student.id,
        )
        .filter(
            (AllowanceOverride.valid_from.is_(None))
            | (AllowanceOverride.valid_from <= now)
        )
        .filter(
            (AllowanceOverride.valid_until.is_(None))
            | (AllowanceOverride.valid_until >= now)
        )
        .scalar()
        or 0
    )

    allowance = base + int(overrides)
    since = _period_start(period, best_group)

    pool_wallet = get_or_create_wallet(db, "college", str(college.id))
    used = (
        db.query(func.coalesce(func.sum(-TokenLedger.amount), 0))
        .filter(
            TokenLedger.wallet_id == pool_wallet.id,
            TokenLedger.kind == "consume",
            TokenLedger.on_behalf_of == str(student.id),
            TokenLedger.created_at >= since,
        )
        .scalar()
        or 0
    )

    return AllowanceView(
        allowance=allowance,
        used_this_period=int(used),
        remaining=max(allowance - int(used), 0),
        period=period,
    )


def _group_active(group: AllowanceGroup, now: datetime) -> bool:
    if group.valid_from is not None and group.valid_from > now:
        return False
    if group.valid_until is not None and group.valid_until < now:
        return False
    return True


# ── Availability ──────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class Availability:
    personal_balance: int
    personal_reserved: int
    college_pool_remaining: int | None
    college_allowance_remaining: int | None

    @property
    def total_available(self) -> int:
        personal = max(self.personal_balance - self.personal_reserved, 0)
        if self.college_pool_remaining is None or self.college_allowance_remaining is None:
            return personal
        from_college = min(self.college_pool_remaining, self.college_allowance_remaining)
        return personal + max(from_college, 0)


def availability(db: Session, student: Student) -> Availability:
    personal = get_or_create_wallet(db, "student", str(student.id))
    college_pool: int | None = None
    allowance_remaining: int | None = None

    college = _active_college(db, student)
    if college is not None:
        pool = get_or_create_wallet(db, "college", str(college.id))
        college_pool = max(int(pool.balance) - int(pool.reserved), 0)
        allowance_remaining = college_allowance(db, student, college).remaining

    return Availability(
        personal_balance=int(personal.balance),
        personal_reserved=int(personal.reserved),
        college_pool_remaining=college_pool,
        college_allowance_remaining=allowance_remaining,
    )


def _active_college(db: Session, student: Student) -> College | None:
    if not student.college_id:
        return None
    college = db.query(College).filter(College.id == student.college_id).first()
    if college is None or not college.is_active or college.deleted_at is not None:
        return None
    return college


# ── Reserve / consume / release ───────────────────────────────────────────────


@dataclass(frozen=True)
class Reservation:
    wallet_id: str
    owner_type: OwnerType
    #: Set when a college pool funded this interview.
    funded_by_college_id: str | None
    remaining_after: int


def reserve_for_interview(
    db: Session, *, student: Student, session_id: str, actor_id: str | None = None
) -> Reservation:
    """Hold one token for a new interview. Must run inside the session-creation transaction.

    College pool first (so students spend the college's allocation before their own),
    then the personal wallet.
    """
    _assert_no_active_interview(db, student, session_id)
    _assert_daily_cap(db, student, session_id)

    college = _active_college(db, student)

    if college is not None:
        view = college_allowance(db, student, college)
        pool = get_or_create_wallet(db, "college", str(college.id), lock=True)
        pool_spendable = int(pool.balance) - int(pool.reserved)
        if view.remaining > 0 and pool_spendable > 0:
            pool.reserved = int(pool.reserved) + 1
            _append(
                db,
                pool,
                kind="reserve",
                amount=0,
                reason=f"Interview {session_id} reserved from college pool",
                actor_id=actor_id,
                session_id=session_id,
                on_behalf_of=str(student.id),
            )
            return Reservation(
                wallet_id=str(pool.id),
                owner_type="college",
                funded_by_college_id=str(college.id),
                remaining_after=pool_spendable - 1,
            )
        if view.remaining <= 0 and pool_spendable > 0:
            # The college has tokens but this student has used their share.
            personal = get_or_create_wallet(db, "student", str(student.id), lock=True)
            if int(personal.balance) - int(personal.reserved) <= 0:
                raise AllowanceExhausted(
                    f"Your college allowance for this {view.period} is used up "
                    f"({view.used_this_period}/{view.allowance}). "
                    "You can buy a personal plan to continue."
                )

    personal = get_or_create_wallet(db, "student", str(student.id), lock=True)
    spendable = int(personal.balance) - int(personal.reserved)
    if spendable <= 0:
        if college is not None:
            raise InsufficientTokens(
                "Your college's token pool is exhausted. Contact your placement officer, "
                "or buy a personal plan to continue."
            )
        raise InsufficientTokens(
            "You have no interview tokens left. Buy a plan to continue."
        )

    personal.reserved = int(personal.reserved) + 1
    _append(
        db,
        personal,
        kind="reserve",
        amount=0,
        reason=f"Interview {session_id} reserved from personal wallet",
        actor_id=actor_id,
        session_id=session_id,
        on_behalf_of=str(student.id),
    )
    return Reservation(
        wallet_id=str(personal.id),
        owner_type="student",
        funded_by_college_id=None,
        remaining_after=spendable - 1,
    )


def consume_for_interview(db: Session, *, session_id: str, reason: str) -> bool:
    """Turn a reservation into a spend. Idempotent — a second call is a no-op."""
    reservation = _find_reservation(db, session_id)
    if reservation is None:
        return False
    if _is_settled(db, session_id):
        return False

    wallet = (
        db.query(TokenWallet)
        .filter(TokenWallet.id == reservation.wallet_id)
        .with_for_update()
        .first()
    )
    if wallet is None:  # pragma: no cover - referential integrity
        return False

    lot = _oldest_live_lot(db, wallet)
    if lot is not None:
        lot.remaining = max(int(lot.remaining) - 1, 0)

    wallet.reserved = max(int(wallet.reserved) - 1, 0)
    wallet.balance = max(int(wallet.balance) - 1, 0)
    _append(
        db,
        wallet,
        kind="consume",
        amount=-1,
        reason=reason,
        session_id=session_id,
        lot_id=lot.id if lot is not None else None,
        on_behalf_of=reservation.on_behalf_of,
    )
    log.info("token_consumed", session_id=session_id, wallet_id=wallet.id)
    return True


def release_for_interview(db: Session, *, session_id: str, reason: str) -> bool:
    """Return a reservation unspent. Idempotent."""
    reservation = _find_reservation(db, session_id)
    if reservation is None:
        return False
    if _is_settled(db, session_id):
        return False

    wallet = (
        db.query(TokenWallet)
        .filter(TokenWallet.id == reservation.wallet_id)
        .with_for_update()
        .first()
    )
    if wallet is None:  # pragma: no cover
        return False

    wallet.reserved = max(int(wallet.reserved) - 1, 0)
    _append(
        db,
        wallet,
        kind="release",
        amount=0,
        reason=reason,
        session_id=session_id,
        on_behalf_of=reservation.on_behalf_of,
    )
    log.info("token_released", session_id=session_id, reason=reason)
    return True


def _find_reservation(db: Session, session_id: str) -> TokenLedger | None:
    return (
        db.query(TokenLedger)
        .filter(TokenLedger.session_id == session_id, TokenLedger.kind == "reserve")
        .first()
    )


def _is_settled(db: Session, session_id: str) -> bool:
    """True once this session's reservation has been consumed or released."""
    return (
        db.query(TokenLedger)
        .filter(
            TokenLedger.session_id == session_id,
            TokenLedger.kind.in_(("consume", "release")),
        )
        .first()
        is not None
    )


def _oldest_live_lot(db: Session, wallet: TokenWallet) -> TokenLot | None:
    """FIFO by earliest expiry; lots with no expiry are used last."""
    return (
        db.query(TokenLot)
        .filter(TokenLot.wallet_id == wallet.id, TokenLot.remaining > 0)
        .order_by(TokenLot.expires_at.is_(None), TokenLot.expires_at.asc())
        .first()
    )


# ── Abuse guards ──────────────────────────────────────────────────────────────


def abandon_stale_sessions(db: Session, *, student_id: str | None = None) -> int:
    """Close interviews left open past the abandonment window, releasing their tokens.

    Nothing marks a session finished when the student closes the tab, so an orphaned
    "active" row blocks every future start for that student. Production accumulated
    sessions stuck active for six months, which locked two real accounts out entirely.

    Caller commits. Releasing is idempotent, and a session with no reservation (one
    started before the token ledger existed) is simply closed.
    """
    cutoff = utcnow() - timedelta(minutes=settings.INTERVIEW_ABANDON_AFTER_MIN)
    query = db.query(InterviewSession).filter(
        InterviewSession.status == "active",
        InterviewSession.started_at < cutoff,
    )
    if student_id is not None:
        query = query.filter(InterviewSession.student_id == student_id)

    closed = 0
    for session in query.all():
        session.status = "abandoned"
        session.ended_at = utcnow()
        release_for_interview(
            db, session_id=str(session.id), reason="Interview abandoned"
        )
        closed += 1

    if closed:
        # Flush explicitly rather than trusting autoflush: the caller immediately queries
        # for active sessions, and a Session configured without autoflush would still see
        # the rows as active and refuse the start.
        db.flush()
        log.info("sessions_abandoned", count=closed, student_id=student_id)
    return closed


def _assert_no_active_interview(
    db: Session, student: Student, new_session_id: str
) -> None:
    # Clear this student's stale sessions first, so an interview they walked away from
    # weeks ago does not refuse them forever. Only genuinely recent ones block a start.
    abandon_stale_sessions(db, student_id=str(student.id))

    active = (
        db.query(InterviewSession)
        .filter(
            InterviewSession.student_id == str(student.id),
            InterviewSession.status == "active",
            InterviewSession.id != new_session_id,
        )
        .first()
    )
    if active is not None:
        raise InterviewAlreadyActive(
            "You already have an interview in progress. Finish or end it first."
        )


def _assert_daily_cap(db: Session, student: Student, new_session_id: str) -> None:
    college = _active_college(db, student)
    cap = int(
        (college.daily_interview_cap if college else None)
        or settings.DEFAULT_DAILY_INTERVIEW_CAP
    )
    since = utcnow().replace(hour=0, minute=0, second=0, microsecond=0)
    today = (
        db.query(func.count(InterviewSession.id))
        .filter(
            InterviewSession.student_id == str(student.id),
            InterviewSession.started_at >= since,
            # The caller creates the session row and *then* reserves, so autoflush makes
            # the row being created visible to this count. Without this exclusion the cap
            # is off by one and a cap of 3 only ever allows 2 interviews.
            InterviewSession.id != new_session_id,
        )
        .scalar()
        or 0
    )
    if int(today) >= cap:
        raise DailyCapReached(
            f"You have reached today's limit of {cap} interviews. Try again tomorrow."
        )


def auto_releases_today(db: Session, student_id: str) -> int:
    """How many reservations we have already handed back to this student today.

    Caps the "our fault, have it back" path so it cannot be farmed by forcing errors.
    """
    since = utcnow().replace(hour=0, minute=0, second=0, microsecond=0)
    return int(
        db.query(func.count(TokenLedger.id))
        .filter(
            TokenLedger.kind == "release",
            TokenLedger.on_behalf_of == student_id,
            TokenLedger.created_at >= since,
        )
        .scalar()
        or 0
    )


def may_auto_release(db: Session, student_id: str) -> bool:
    return auto_releases_today(db, student_id) < settings.MAX_AUTO_RELEASES_PER_DAY

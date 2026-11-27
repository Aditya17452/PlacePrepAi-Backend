"""Token ledger behaviour (PLAN.md §8).

The money rules, asserted: reserve at start, consume at the threshold, release if it was
our fault, never go negative, never double-spend, and allowances cap drawing from a
college pool without ever moving tokens into a student's wallet.
"""

from __future__ import annotations

import pytest

from app.models import TokenLedger, TokenLot, TokenWallet
from app.services import token_service
from tests.conftest import make_college, make_student


def _wallet(db, owner_type: str, owner_id: str) -> TokenWallet:
    return token_service.get_or_create_wallet(db, owner_type, owner_id)


# ── Grants ────────────────────────────────────────────────────────────────────


def test_grant_creates_a_lot_and_a_ledger_entry(db):
    student = make_student(db)
    token_service.grant(
        db,
        owner_type="student",
        owner_id=str(student.id),
        qty=5,
        reason="test grant",
        actor_id=None,
    )
    db.commit()

    wallet = _wallet(db, "student", str(student.id))
    assert wallet.balance == 5
    lots = db.query(TokenLot).filter(TokenLot.wallet_id == wallet.id).all()
    assert sum(lot.remaining for lot in lots) == 5
    entries = db.query(TokenLedger).filter(TokenLedger.wallet_id == wallet.id).all()
    assert any(e.kind == "grant" and e.amount == 5 for e in entries)


def test_grant_requires_a_reason(db):
    student = make_student(db)
    with pytest.raises(token_service.TokenError):
        token_service.grant(
            db,
            owner_type="student",
            owner_id=str(student.id),
            qty=1,
            reason="   ",
            actor_id=None,
        )


def test_grant_rejects_a_non_positive_quantity(db):
    student = make_student(db)
    with pytest.raises(token_service.TokenError):
        token_service.grant(
            db,
            owner_type="student",
            owner_id=str(student.id),
            qty=0,
            reason="nope",
            actor_id=None,
        )


# ── Reserve, consume, release ─────────────────────────────────────────────────


def test_reserve_then_consume_spends_exactly_one(db):
    student = make_student(db, tokens=3)
    token_service.reserve_for_interview(
        db, student=student, session_id="session-1", actor_id=str(student.id)
    )
    db.commit()

    wallet = _wallet(db, "student", str(student.id))
    assert wallet.balance == 3, "a reservation must not reduce the balance yet"
    assert wallet.reserved == 1

    assert token_service.consume_for_interview(
        db, session_id="session-1", reason="threshold reached"
    )
    db.commit()

    db.refresh(wallet)
    assert wallet.balance == 2
    assert wallet.reserved == 0


def test_release_returns_the_reservation_unspent(db):
    student = make_student(db, tokens=2)
    token_service.reserve_for_interview(db, student=student, session_id="session-2")
    db.commit()

    assert token_service.release_for_interview(
        db, session_id="session-2", reason="server error"
    )
    db.commit()

    wallet = _wallet(db, "student", str(student.id))
    assert wallet.balance == 2, "a released reservation costs nothing"
    assert wallet.reserved == 0


def test_consume_is_idempotent(db):
    student = make_student(db, tokens=2)
    token_service.reserve_for_interview(db, student=student, session_id="session-3")
    db.commit()

    assert token_service.consume_for_interview(db, session_id="session-3", reason="first")
    db.commit()
    assert not token_service.consume_for_interview(
        db, session_id="session-3", reason="second"
    ), "a repeated consume must be a no-op"
    db.commit()

    wallet = _wallet(db, "student", str(student.id))
    assert wallet.balance == 1


def test_release_after_consume_does_nothing(db):
    student = make_student(db, tokens=2)
    token_service.reserve_for_interview(db, student=student, session_id="session-4")
    db.commit()
    token_service.consume_for_interview(db, session_id="session-4", reason="spent")
    db.commit()

    assert not token_service.release_for_interview(
        db, session_id="session-4", reason="too late"
    )
    db.commit()
    assert _wallet(db, "student", str(student.id)).balance == 1


def test_reserve_refuses_with_no_tokens(db):
    student = make_student(db, tokens=0)
    with pytest.raises(token_service.InsufficientTokens):
        token_service.reserve_for_interview(db, student=student, session_id="session-5")


def test_reserve_refuses_a_second_concurrent_interview(db):
    from app.models import InterviewSession

    student = make_student(db, tokens=5)
    db.add(
        InterviewSession(
            id="active-session", student_id=str(student.id), jd_id="jd", status="active"
        )
    )
    db.commit()

    with pytest.raises(token_service.InterviewAlreadyActive):
        token_service.reserve_for_interview(db, student=student, session_id="session-6")


def test_daily_cap_is_enforced(db):
    from app.models import InterviewSession, utcnow

    student = make_student(db, tokens=10)
    for index in range(3):
        db.add(
            InterviewSession(
                id=f"done-{index}",
                student_id=str(student.id),
                jd_id="jd",
                status="completed",
                started_at=utcnow(),
            )
        )
    db.commit()

    with pytest.raises(token_service.DailyCapReached):
        token_service.reserve_for_interview(db, student=student, session_id="session-7")


def test_balance_never_goes_negative(db):
    student = make_student(db, tokens=1)
    token_service.reserve_for_interview(db, student=student, session_id="session-8")
    db.commit()
    token_service.consume_for_interview(db, session_id="session-8", reason="spent")
    db.commit()

    wallet = _wallet(db, "student", str(student.id))
    assert wallet.balance == 0
    # No reservation exists, so a further consume cannot push it below zero.
    assert not token_service.consume_for_interview(
        db, session_id="session-8", reason="again"
    )
    assert _wallet(db, "student", str(student.id)).balance == 0


# ── FIFO by expiry ────────────────────────────────────────────────────────────


def test_consumption_takes_the_earliest_expiring_lot_first(db):
    student = make_student(db)
    token_service.grant(
        db,
        owner_type="student",
        owner_id=str(student.id),
        qty=1,
        reason="expires later",
        actor_id=None,
        validity_days=90,
    )
    token_service.grant(
        db,
        owner_type="student",
        owner_id=str(student.id),
        qty=1,
        reason="expires sooner",
        actor_id=None,
        validity_days=10,
    )
    db.commit()

    token_service.reserve_for_interview(db, student=student, session_id="session-9")
    db.commit()
    token_service.consume_for_interview(db, session_id="session-9", reason="spent")
    db.commit()

    wallet = _wallet(db, "student", str(student.id))
    lots = (
        db.query(TokenLot)
        .filter(TokenLot.wallet_id == wallet.id)
        .order_by(TokenLot.expires_at)
        .all()
    )
    assert lots[0].remaining == 0, "the soonest-expiring lot should be used first"
    assert lots[1].remaining == 1


# ── College pools and allowances ──────────────────────────────────────────────


def test_college_pool_is_spent_before_the_personal_wallet(db):
    college = make_college(db, pool=50)
    student = make_student(db, college_id=str(college.id), tokens=5)

    reservation = token_service.reserve_for_interview(
        db, student=student, session_id="session-10"
    )
    db.commit()

    assert reservation.owner_type == "college"
    assert reservation.funded_by_college_id == str(college.id)
    assert _wallet(db, "student", str(student.id)).reserved == 0


def test_allowance_caps_drawing_from_the_pool(db):
    """A big pool does not mean one student may take all of it."""
    from app.models import AllowanceGroup, AllowanceMember, InterviewSession, utcnow

    college = make_college(db, pool=1_000)
    college.daily_interview_cap = 50
    student = make_student(db, college_id=str(college.id), tokens=0)

    group = AllowanceGroup(
        college_id=str(college.id),
        name="tight",
        filters={},
        per_student_allowance=1,
        period="month",
    )
    db.add(group)
    db.flush()
    db.add(AllowanceMember(group_id=group.id, student_id=str(student.id)))
    db.commit()

    view = token_service.college_allowance(db, student, college)
    assert view.allowance == 1
    assert view.remaining == 1

    token_service.reserve_for_interview(db, student=student, session_id="session-11")
    db.commit()
    token_service.consume_for_interview(db, session_id="session-11", reason="spent")
    db.add(
        InterviewSession(
            id="session-11",
            student_id=str(student.id),
            jd_id="jd",
            status="completed",
            started_at=utcnow(),
        )
    )
    db.commit()

    view = token_service.college_allowance(db, student, college)
    assert view.used_this_period == 1
    assert view.remaining == 0

    # Allowance spent and no personal tokens: refused, with a message that points at the
    # personal plan rather than blaming the college.
    with pytest.raises(token_service.AllowanceExhausted):
        token_service.reserve_for_interview(
            db, student=student, session_id="session-12"
        )


def test_allowance_takes_the_most_generous_group_plus_overrides(db):
    from app.models import AllowanceGroup, AllowanceMember, AllowanceOverride

    college = make_college(db, pool=100)
    student = make_student(db, college_id=str(college.id))

    for allowance in (2, 7):
        group = AllowanceGroup(
            college_id=str(college.id),
            name=f"group-{allowance}",
            filters={},
            per_student_allowance=allowance,
            period="month",
        )
        db.add(group)
        db.flush()
        db.add(AllowanceMember(group_id=group.id, student_id=str(student.id)))

    db.add(
        AllowanceOverride(
            college_id=str(college.id),
            student_id=str(student.id),
            kind="drive",
            extra_allowance=5,
            reason="TCS drive",
        )
    )
    db.commit()

    view = token_service.college_allowance(db, student, college)
    # max(2, 7) + 5, not 2 + 7 + 5.
    assert view.allowance == 12


def test_allowance_falls_back_to_the_college_default(db):
    college = make_college(db, pool=10)
    student = make_student(db, college_id=str(college.id))
    view = token_service.college_allowance(db, student, college)
    assert view.allowance == 4, "the default is 4 per student per month"


def test_availability_combines_personal_and_college(db):
    college = make_college(db, pool=3)
    student = make_student(db, college_id=str(college.id), tokens=2)

    avail = token_service.availability(db, student)
    assert avail.personal_balance == 2
    assert avail.college_pool_remaining == 3
    assert avail.college_allowance_remaining == 4
    # Pool has 3, allowance allows 4 — the pool is the binding constraint.
    assert avail.total_available == 5


def test_inactive_college_is_ignored(db):
    college = make_college(db, pool=10)
    college.is_active = False
    student = make_student(db, college_id=str(college.id), tokens=1)
    db.commit()

    avail = token_service.availability(db, student)
    assert avail.college_pool_remaining is None
    assert avail.total_available == 1


# ── Revocation ────────────────────────────────────────────────────────────────


def test_revoke_cannot_take_reserved_or_spent_tokens(db):
    student = make_student(db, tokens=3)
    token_service.reserve_for_interview(db, student=student, session_id="session-13")
    db.commit()

    taken = token_service.revoke(
        db,
        owner_type="student",
        owner_id=str(student.id),
        qty=5,
        reason="over-revoke attempt",
        actor_id=None,
    )
    db.commit()

    # 3 held, 1 reserved, so only 2 are revocable.
    assert taken == 2
    wallet = _wallet(db, "student", str(student.id))
    assert wallet.balance == 1
    assert wallet.reserved == 1

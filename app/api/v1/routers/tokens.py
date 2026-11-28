"""A student's own token wallet and history (PLAN.md §8).

Student-facing only. Admin grants live in the admin router and college allowance
distribution in the college-admin router, so the three audiences cannot be confused for
one another when reading the routes.
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, status

from app.api.deps import CurrentUser, DbSession
from app.core.logging_setup import get_logger
from app.models import (
    Student,
    TokenLedger,
    TokenLot,
)
from app.schemas import (
    LedgerEntryOut,
    WalletResponse,
)
from app.services import token_service

log = get_logger("tokens_api")

router = APIRouter(tags=["tokens"])


# ── Student: own wallet ───────────────────────────────────────────────────────


@router.get("/api/tokens/wallet", response_model=WalletResponse)
async def my_wallet(principal: CurrentUser, db: DbSession) -> WalletResponse:
    student = db.query(Student).filter(Student.id == principal.id).first()
    if student is None:  # pragma: no cover
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Not found")

    avail = token_service.availability(db, student)
    wallet = token_service.get_or_create_wallet(db, "student", principal.id)

    next_lot = (
        db.query(TokenLot)
        .filter(
            TokenLot.wallet_id == wallet.id,
            TokenLot.remaining > 0,
            TokenLot.expires_at.isnot(None),
        )
        .order_by(TokenLot.expires_at.asc())
        .first()
    )
    db.commit()

    return WalletResponse(
        personal_balance=avail.personal_balance,
        personal_reserved=avail.personal_reserved,
        college_allowance_remaining=avail.college_allowance_remaining,
        college_pool_remaining=avail.college_pool_remaining,
        total_available=avail.total_available,
        next_expiry=next_lot.expires_at if next_lot else None,
        next_expiry_qty=int(next_lot.remaining) if next_lot else None,
    )


@router.get("/api/tokens/ledger", response_model=list[LedgerEntryOut])
async def my_ledger(
    principal: CurrentUser, db: DbSession, limit: int = 50
) -> list[LedgerEntryOut]:
    """The caller's own token history.

    Includes spends funded by their college, which live on the college's wallet — matched
    by `on_behalf_of` so a student can see them without being able to read the pool.
    """
    wallet = token_service.get_or_create_wallet(db, "student", principal.id)
    db.commit()

    rows = (
        db.query(TokenLedger)
        .filter(
            (TokenLedger.wallet_id == wallet.id)
            | (TokenLedger.on_behalf_of == principal.id)
        )
        .order_by(TokenLedger.created_at.desc())
        .limit(min(max(limit, 1), 200))
        .all()
    )
    return [LedgerEntryOut.model_validate(r) for r in rows]


# ── Admin: creating and removing tokens ───────────────────────────────────────






# ── College admin: the pool and its distribution ──────────────────────────────











"""Scheduled maintenance endpoints, called by Vercel Cron.

These replace what `asyncio.create_task` and a long-lived process used to do. They are
HTTP endpoints because that is how scheduling works on a serverless host, which means
they must be authenticated: Vercel sends `Authorization: Bearer $CRON_SECRET`, and
without a matching secret configured the endpoints refuse to run rather than being open
to the internet.
"""

from __future__ import annotations

from datetime import timedelta

from fastapi import APIRouter, HTTPException, Request, status
from sqlalchemy import func

from api.deps import DbSession
from core.config import settings
from core.logging_setup import get_logger
from models.database import utcnow
from models.proctoring import ProctoringEvent
from models.tokens import TokenLedger, TokenLot, TokenWallet
from services import jobs, storage

log = get_logger("cron")

router = APIRouter(prefix="/api/cron", tags=["ops"], include_in_schema=False)


def _authorise(request: Request) -> None:
    secret = settings.CRON_SECRET
    if not secret:
        # Fail closed. An unauthenticated maintenance endpoint is worse than no cron.
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            "CRON_SECRET is not configured; scheduled jobs are disabled.",
        )
    header = request.headers.get("Authorization", "")
    expected = f"Bearer {secret}"
    if len(header) != len(expected) or header != expected:
        log.warning("cron_unauthorised")
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Unauthorised")


@router.post("/drain-jobs")
@router.get("/drain-jobs")
async def drain_jobs(request: Request, db: DbSession) -> dict:
    """Run queued background work: evaluations, JD matches, context builds."""
    _authorise(request)
    requeued = jobs.requeue_stale(db)
    result = await jobs.drain(db, limit=10)
    return {"requeued_stale": requeued, **result}


@router.post("/maintenance")
@router.get("/maintenance")
async def maintenance(request: Request, db: DbSession) -> dict:
    """Daily housekeeping: expire token lots, purge old proctoring snapshots."""
    _authorise(request)
    expired = _expire_token_lots(db)
    purged = await _purge_snapshots(db)
    return {"lots_expired": expired, "snapshots_purged": purged}


def _expire_token_lots(db: DbSession) -> int:  # type: ignore[valid-type]
    """Zero out lots past their expiry and write the ledger entry for it.

    Without this, an expired lot still counts toward `token_wallets.balance` and students
    would appear to have tokens they cannot spend.
    """
    now = utcnow()
    lots = (
        db.query(TokenLot)
        .filter(
            TokenLot.remaining > 0,
            TokenLot.expires_at.isnot(None),
            TokenLot.expires_at < now,
        )
        .limit(500)
        .all()
    )

    expired_total = 0
    for lot in lots:
        quantity = int(lot.remaining)
        wallet = (
            db.query(TokenWallet).filter(TokenWallet.id == lot.wallet_id).first()
        )
        if wallet is None:  # pragma: no cover
            continue

        # Never expire tokens that are reserved for an interview in progress.
        spendable = int(wallet.balance) - int(wallet.reserved)
        quantity = min(quantity, max(spendable, 0))
        if quantity <= 0:
            continue

        lot.remaining = int(lot.remaining) - quantity
        wallet.balance = max(int(wallet.balance) - quantity, 0)
        db.add(
            TokenLedger(
                wallet_id=wallet.id,
                lot_id=lot.id,
                kind="expire",
                amount=-quantity,
                reason=f"Lot expired on {lot.expires_at:%Y-%m-%d}",
            )
        )
        expired_total += quantity

    if expired_total:
        db.commit()
        log.info("token_lots_expired", tokens=expired_total, lots=len(lots))
    return expired_total


async def _purge_snapshots(db: DbSession) -> int:  # type: ignore[valid-type]
    """Delete proctoring snapshots past the retention period (14 days, PLAN.md §0).

    Stated in the consent notice, so this is a commitment rather than a cleanup.
    """
    cutoff = utcnow() - timedelta(days=settings.SNAPSHOT_RETENTION_DAYS)
    rows = (
        db.query(ProctoringEvent)
        .filter(
            ProctoringEvent.snapshot_path.isnot(None),
            ProctoringEvent.purged_at.is_(None),
            ProctoringEvent.server_ts < cutoff,
        )
        .limit(500)
        .all()
    )
    if not rows:
        return 0

    paths = [row.snapshot_path for row in rows if row.snapshot_path]
    if storage.is_configured() and paths:
        try:
            await storage.delete(
                bucket=settings.SUPABASE_SNAPSHOT_BUCKET, paths=paths
            )
        except storage.StorageUnavailable as exc:
            # Leave purged_at unset so the next run retries; never mark it done on faith.
            log.error("snapshot_purge_failed", error=str(exc)[:160])
            return 0

    now = utcnow()
    for row in rows:
        row.purged_at = now
        row.snapshot_path = None
    db.commit()
    log.info("snapshots_purged", count=len(rows))
    return len(rows)


@router.get("/stats")
async def cron_stats(request: Request, db: DbSession) -> dict:
    """What the scheduled jobs have to do right now. Useful when debugging a backlog."""
    _authorise(request)
    from models.billing import JobQueue

    pending = (
        db.query(JobQueue.status, func.count(JobQueue.id))
        .group_by(JobQueue.status)
        .all()
    )
    return {"job_queue": {status: int(count) for status, count in pending}}

"""Durable background work.

`asyncio.create_task` was used for the two slowest operations — grading a finished
interview and matching a pasted JD against a resume. Both are lost when the process
restarts, which left `session_scores.evaluation_status = 'pending'` forever while the
report page polled every four seconds in an unbounded recursion. On Vercel the process
always goes away, so the pattern cannot work at all.

Jobs are rows now. Enqueue inside the transaction that created the work, then a
cron-triggered worker claims and runs them with retries and a dead-letter state.
"""

from __future__ import annotations

import socket
from collections.abc import Awaitable, Callable
from datetime import timedelta
from typing import Any

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from core.logging_setup import get_logger
from models.billing import JobQueue
from models.database import utcnow

log = get_logger("jobs")

#: Registered handlers, filled by `register`.
_HANDLERS: dict[str, Callable[[Session, dict[str, Any]], Awaitable[None]]] = {}


def register(kind: str):  # type: ignore[no-untyped-def]
    def decorator(fn):  # type: ignore[no-untyped-def]
        _HANDLERS[kind] = fn
        return fn

    return decorator


def enqueue(
    db: Session,
    *,
    kind: str,
    payload: dict[str, Any],
    dedupe_key: str | None = None,
    delay_sec: int = 0,
    max_attempts: int = 5,
) -> JobQueue | None:
    """Add a job. Does not commit — it joins the caller's transaction.

    `dedupe_key` makes enqueueing idempotent, so a retried request cannot schedule two
    evaluations for the same interview. Returns None when the job already exists.
    """
    job = JobQueue(
        kind=kind,
        payload=payload,
        dedupe_key=dedupe_key,
        max_attempts=max_attempts,
        run_after=utcnow() + timedelta(seconds=delay_sec),
        status="queued",
    )
    db.add(job)
    try:
        db.flush()
    except IntegrityError:
        db.rollback()
        log.info("job_already_queued", kind=kind, dedupe_key=dedupe_key)
        return None
    return job


async def drain(db: Session, *, limit: int = 5) -> dict[str, int]:
    """Claim and run up to `limit` due jobs. Returns a count per outcome.

    Called by the cron endpoint. Claiming uses `SKIP LOCKED` on Postgres so two
    concurrent cron invocations never run the same job.
    """
    worker = f"{socket.gethostname()[:40]}"
    results = {"ran": 0, "failed": 0, "dead": 0}

    for _ in range(limit):
        job = _claim(db, worker)
        if job is None:
            break

        handler = _HANDLERS.get(job.kind)
        if handler is None:
            job.status = "dead"
            job.last_error = f"No handler registered for {job.kind}"
            job.finished_at = utcnow()
            db.commit()
            results["dead"] += 1
            continue

        try:
            await handler(db, job.payload or {})
            job.status = "done"
            job.finished_at = utcnow()
            job.last_error = None
            db.commit()
            results["ran"] += 1
        except Exception as exc:
            db.rollback()
            # Re-read: the rollback above detached our in-memory copy.
            job = db.query(JobQueue).filter(JobQueue.id == job.id).first()
            if job is None:  # pragma: no cover
                continue
            job.attempts = int(job.attempts) + 1
            job.last_error = str(exc)[:1000]
            job.locked_at = None
            job.locked_by = None
            if job.attempts >= int(job.max_attempts):
                job.status = "dead"
                job.finished_at = utcnow()
                results["dead"] += 1
                log.error("job_dead", kind=job.kind, job_id=job.id, error=job.last_error)
            else:
                job.status = "queued"
                # Exponential backoff, so a flapping dependency is not hammered.
                job.run_after = utcnow() + timedelta(seconds=30 * (2 ** job.attempts))
                results["failed"] += 1
                log.warning(
                    "job_retry",
                    kind=job.kind,
                    job_id=job.id,
                    attempt=job.attempts,
                    error=job.last_error[:200],
                )
            db.commit()

    return results


def _claim(db: Session, worker: str) -> JobQueue | None:
    now = utcnow()
    query = (
        db.query(JobQueue)
        .filter(JobQueue.status == "queued", JobQueue.run_after <= now)
        .order_by(JobQueue.run_after)
    )
    # skip_locked is a no-op on SQLite, which is single-writer anyway.
    job = query.with_for_update(skip_locked=True).first()
    if job is None:
        return None

    job.status = "running"
    job.locked_at = now
    job.locked_by = worker
    db.commit()
    return job


def requeue_stale(db: Session, *, older_than_minutes: int = 15) -> int:
    """Return jobs stuck in `running` to the queue.

    A function killed mid-job leaves its row claimed forever; this is the sweeper that
    un-sticks them.
    """
    cutoff = utcnow() - timedelta(minutes=older_than_minutes)
    stale = (
        db.query(JobQueue)
        .filter(JobQueue.status == "running", JobQueue.locked_at < cutoff)
        .all()
    )
    for job in stale:
        job.status = "queued"
        job.locked_at = None
        job.locked_by = None
    if stale:
        db.commit()
        log.warning("jobs_requeued_stale", count=len(stale))
    return len(stale)

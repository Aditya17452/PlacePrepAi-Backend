"""Append-only audit trail (PLAN.md §7.2).

Every admin and college-admin action, every token movement, and every report a college
admin opens. Writing is best-effort by design: a failure to audit logs an error but does
not fail the request that was already authorised. Reading is admin-only and there is no
update or delete path anywhere in the codebase.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy.orm import Session

from app.core.logging_setup import current_request_id, get_logger

log = get_logger("audit")


def record(
    db: Session,
    *,
    action: str,
    actor_id: str | None = None,
    actor_role: str | None = None,
    target: str | None = None,
    detail: dict[str, Any] | None = None,
    ip: str | None = None,
    commit: bool = False,
) -> None:
    """Append one audit row.

    By default this does *not* commit, so the audit row lands in the same transaction as
    the change it describes — a rolled-back token grant leaves no misleading audit entry.
    Pass `commit=True` for read-only events (e.g. a report view) that have no surrounding
    transaction.
    """
    try:
        from app.models import ApiAuditLog

        db.add(
            ApiAuditLog(
                actor_id=actor_id,
                actor_role=actor_role,
                action=action,
                target=target,
                detail=detail,
                ip=ip,
                request_id=current_request_id(),
            )
        )
        if commit:
            db.commit()
    except Exception as exc:  # pragma: no cover - auditing must not break the request
        log.error("audit_write_failed", action=action, error=str(exc))


def target_for(kind: str, identifier: str | None) -> str | None:
    return f"{kind}:{identifier}" if identifier else None

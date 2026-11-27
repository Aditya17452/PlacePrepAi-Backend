"""Recording and checking consent (India DPDP Act; PLAN.md §4 Phase 4).

Consent is versioned. A stored consent only counts as current while its version matches
the version of the notice we are showing now — so changing the notice text and bumping
`CONSENT_VERSION_*` makes everyone re-consent instead of silently carrying an agreement
to different terms.

What is stored: who, which kind, which version, when, the IP and the user agent. The IP
and user agent are the evidence that the consent was given by that person at that moment;
nothing else about the request is kept.

Withdrawal is a timestamp, not a delete, because "they consented and later withdrew" and
"they never consented" are different facts and we need to be able to tell them apart.
"""

from __future__ import annotations

from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.logging_setup import get_logger
from app.models import Consent, utcnow

log = get_logger("consent")

ConsentKind = str  # 'proctoring' | 'profile_processing' | 'college_sharing'

#: The version of each notice that is currently in force.
CURRENT_VERSIONS: dict[str, str] = {
    "proctoring": settings.CONSENT_VERSION_PROCTORING,
    "profile_processing": settings.CONSENT_VERSION_PROFILE,
    "college_sharing": settings.CONSENT_VERSION_COLLEGE_SHARING,
}


def current_version(kind: ConsentKind) -> str:
    return CURRENT_VERSIONS.get(kind, "unknown")


def grant(
    db: Session,
    *,
    student_id: str,
    kind: ConsentKind,
    version: str,
    ip: str | None,
    user_agent: str | None,
) -> Consent:
    """Record a consent. Caller commits.

    Re-granting the same kind supersedes any earlier live consent, so a student always
    has at most one active consent per kind and the history stays readable.
    """
    now = utcnow()
    db.query(Consent).filter(
        Consent.student_id == student_id,
        Consent.kind == kind,
        Consent.revoked_at.is_(None),
    ).update({"revoked_at": now}, synchronize_session=False)

    record = Consent(
        student_id=student_id,
        kind=kind,
        version=version,
        granted_at=now,
        ip=ip,
        # Truncated to the column width; a long UA must not fail the write.
        user_agent=(user_agent or "")[:400] or None,
    )
    db.add(record)
    log.info("consent_granted", student_id=student_id, kind=kind, version=version)
    return record


def revoke(db: Session, *, student_id: str, kind: ConsentKind) -> int:
    """Withdraw consent. Returns how many live consents were closed. Caller commits."""
    closed = (
        db.query(Consent)
        .filter(
            Consent.student_id == student_id,
            Consent.kind == kind,
            Consent.revoked_at.is_(None),
        )
        .update({"revoked_at": utcnow()}, synchronize_session=False)
    )
    if closed:
        log.info("consent_revoked", student_id=student_id, kind=kind)
    return int(closed)


def latest(db: Session, *, student_id: str, kind: ConsentKind) -> Consent | None:
    """The most recent live consent of this kind, whatever its version."""
    return (
        db.query(Consent)
        .filter(
            Consent.student_id == student_id,
            Consent.kind == kind,
            Consent.revoked_at.is_(None),
        )
        .order_by(Consent.granted_at.desc())
        .first()
    )


def has_current(db: Session, *, student_id: str, kind: ConsentKind) -> bool:
    """True only for a live consent to the notice version in force right now."""
    record = latest(db, student_id=student_id, kind=kind)
    return record is not None and record.version == current_version(kind)


def status(db: Session, *, student_id: str) -> list[dict]:
    """Every kind, with whether this student's consent is current.

    The UI uses this to decide which consent screens to show, rather than guessing from
    local state that survives a logout.
    """
    out: list[dict] = []
    for kind, version in CURRENT_VERSIONS.items():
        record = latest(db, student_id=student_id, kind=kind)
        out.append(
            {
                "kind": kind,
                "current_version": version,
                "granted_version": record.version if record else None,
                "granted_at": record.granted_at if record else None,
                "is_current": bool(record and record.version == version),
            }
        )
    return out

"""Consent endpoints.

PR #2 required proctoring consent in the UI but had nowhere to record it, so the
agreement existed only in the browser — which is not a record of consent at all under the
DPDP Act. This stores it server-side with the IP and user agent, and
`/interviews/start` refuses a proctored session without a current one.
"""

from __future__ import annotations

from fastapi import APIRouter, Request

from app.api.deps import CurrentUser, DbSession
from app.core import audit, ratelimit
from app.core.logging_setup import get_logger
from app.schemas import ConsentRequest, ConsentStatusOut, SimpleOk
from app.services import consent

log = get_logger("consents_api")

router = APIRouter(prefix="/api/consents", tags=["consents"])


@router.get("", response_model=list[ConsentStatusOut])
async def my_consents(
    principal: CurrentUser, db: DbSession
) -> list[ConsentStatusOut]:
    """Which consents this student has, and whether each is for the current notice."""
    rows = consent.status(db, student_id=principal.id)
    db.commit()
    return [ConsentStatusOut.model_validate(row) for row in rows]


@router.post("", response_model=SimpleOk, status_code=201)
async def record_consent(
    body: ConsentRequest, request: Request, principal: CurrentUser, db: DbSession
) -> SimpleOk:
    """Grant or withdraw a consent.

    The version is taken from the server, not the request: a client that sent an old
    version string would otherwise record agreement to a notice it never displayed.
    The request's version is still read, so a stale client is told to reload rather than
    silently recording the wrong thing.
    """
    await ratelimit.limit_by_user(principal.id, "consents", "30/3600")

    expected = consent.current_version(body.kind)
    if body.version != expected:
        log.info(
            "consent_version_stale",
            student_id=principal.id,
            kind=body.kind,
            sent=body.version,
            expected=expected,
        )

    ip = ratelimit.client_ip(request)
    user_agent = request.headers.get("User-Agent")

    if body.granted:
        consent.grant(
            db,
            student_id=principal.id,
            kind=body.kind,
            version=expected,
            ip=ip,
            user_agent=user_agent,
        )
        message = "Consent recorded."
    else:
        consent.revoke(db, student_id=principal.id, kind=body.kind)
        message = "Consent withdrawn."

    audit.record(
        db,
        action="consent.granted" if body.granted else "consent.revoked",
        actor_id=principal.id,
        actor_role=principal.role.value,
        detail={"kind": body.kind, "version": expected},
        ip=ip,
    )
    db.commit()
    return SimpleOk(ok=True, message=message)

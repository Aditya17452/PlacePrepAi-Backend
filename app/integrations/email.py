"""Transactional email.

Resend is implemented; no account is configured yet, which is why
`app.services.notify` treats "no provider" as an explicit 503 in production rather than
silently claiming a code was sent.
"""

from __future__ import annotations

import httpx

from app.core.config import settings
from app.core.logging_setup import get_logger

log = get_logger("integrations.email")

SUBJECTS = {
    "email_otp": "Your PlacePrep AI verification code",
    "magic_link": "Sign in to PlacePrep AI",
    "admin_invite": "You have been invited to PlacePrep AI",
}


class EmailSendFailed(RuntimeError):
    pass


def is_configured() -> bool:
    return bool(settings.RESEND_API_KEY)


async def send_code(*, to: str, code: str, purpose: str) -> None:
    if not is_configured():
        raise EmailSendFailed("No email provider configured (set RESEND_API_KEY)")

    body = (
        f"Your PlacePrep AI code is {code}\n\n"
        "It expires in 10 minutes and can be used once.\n"
        "If you did not request this, you can ignore this email."
    )

    async with httpx.AsyncClient(timeout=10.0) as client:
        resp = await client.post(
            "https://api.resend.com/emails",
            headers={"Authorization": f"Bearer {settings.RESEND_API_KEY}"},
            json={
                "from": settings.EMAIL_FROM,
                "to": [to],
                "subject": SUBJECTS.get(purpose, "PlacePrep AI"),
                "text": body,
            },
        )
        if resp.status_code >= 400:
            # Status only: the body can echo the recipient address.
            log.error("send_failed", status=resp.status_code)
            raise EmailSendFailed(f"Provider rejected the request ({resp.status_code})")

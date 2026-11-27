"""Sending OTPs and links by email and SMS.

No provider is wired up yet — picking one is an open decision (see PLAN.md). This module
makes that state explicit rather than pretending:

* **local**: the code is written to the log so development works without an account.
* **production with no provider**: the send raises, the endpoint returns 503, and nobody
  is left staring at a "code sent" message for a code that was never sent.

Codes are never logged outside `ENV=local`.
"""

from __future__ import annotations

from core.config import settings
from core.logging_setup import get_logger

log = get_logger("notify")


class NotificationUnavailable(RuntimeError):
    """No delivery provider is configured for this channel."""


async def send_email_code(*, to: str, code: str, purpose: str) -> None:
    if settings.RESEND_API_KEY:
        await _send_via_resend(to=to, code=code, purpose=purpose)
        return

    if settings.ENV == "local":
        log.warning("email_code_not_sent_local", to=to, purpose=purpose, code=code)
        return

    raise NotificationUnavailable(
        "No email provider configured (set RESEND_API_KEY)"
    )


async def send_sms_code(*, to: str, code: str, purpose: str) -> None:
    if settings.SMS_API_KEY:
        await _send_via_sms_provider(to=to, code=code, purpose=purpose)
        return

    if settings.ENV == "local":
        log.warning("sms_code_not_sent_local", to=to, purpose=purpose, code=code)
        return

    raise NotificationUnavailable("No SMS provider configured (set SMS_API_KEY)")


async def _send_via_resend(*, to: str, code: str, purpose: str) -> None:
    import httpx

    subject = {
        "email_otp": "Your PlacePrep AI verification code",
        "magic_link": "Sign in to PlacePrep AI",
        "admin_invite": "You have been invited to PlacePrep AI",
    }.get(purpose, "PlacePrep AI")

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
                "subject": subject,
                "text": body,
            },
        )
        if resp.status_code >= 400:
            log.error("email_send_failed", status=resp.status_code)
            raise NotificationUnavailable("Email provider rejected the request")


async def _send_via_sms_provider(*, to: str, code: str, purpose: str) -> None:
    """Placeholder for the chosen Indian SMS gateway (MSG91 / Gupshup / Twilio).

    Deliberately not guessed: the request shape and the DLT template registration differ
    per provider, and a wrong guess here fails silently in production. Wire this up once
    the provider is chosen.
    """
    raise NotificationUnavailable(
        "SMS provider not implemented yet — choose a gateway (MSG91 / Gupshup / Twilio) "
        "and complete services/notify._send_via_sms_provider"
    )

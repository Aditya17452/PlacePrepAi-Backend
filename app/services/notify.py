"""Deciding how a one-time code reaches someone.

The vendor calls live in `app.integrations.email` / `.sms`; this module holds the policy,
which is the part worth being explicit about:

* **local**: the code goes to the log, so development works without an account.
* **production with no provider**: the send raises, the endpoint returns 503, and nobody
  is left waiting for a code that was never sent.

Codes are only ever logged when `ENV=local`.
"""

from __future__ import annotations

from app.core.config import settings
from app.core.logging_setup import get_logger
from app.integrations import email as email_provider
from app.integrations import sms as sms_provider

log = get_logger("notify")


class NotificationUnavailable(RuntimeError):
    """No delivery provider is configured or implemented for this channel."""


async def send_email_code(*, to: str, code: str, purpose: str) -> None:
    if email_provider.is_configured():
        try:
            await email_provider.send_code(to=to, code=code, purpose=purpose)
        except email_provider.EmailSendFailed as exc:
            raise NotificationUnavailable(str(exc)) from exc
        return

    if settings.ENV == "local":
        log.warning("email_code_not_sent_local", to=to, purpose=purpose, code=code)
        return

    raise NotificationUnavailable("No email provider configured (set RESEND_API_KEY)")


async def send_sms_code(*, to: str, code: str, purpose: str) -> None:
    if sms_provider.is_configured():
        try:
            await sms_provider.send_code(to=to, code=code, purpose=purpose)
        except sms_provider.SmsSendFailed as exc:
            raise NotificationUnavailable(str(exc)) from exc
        return

    if settings.ENV == "local":
        log.warning("sms_code_not_sent_local", to=to, purpose=purpose, code=code)
        return

    raise NotificationUnavailable("No SMS provider configured (set SMS_API_KEY)")

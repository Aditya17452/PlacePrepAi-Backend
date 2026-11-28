"""SMS — not implemented yet, deliberately.

Phone OTP gates the free tier, so this matters, but the request shape and the DLT template
registration differ per Indian gateway (MSG91, Gupshup, Twilio). A guessed implementation
would fail in production while looking finished here, so this raises until a provider is
chosen — and `app.services.notify` turns that into a 503 rather than a false "code sent".

To finish: pick a gateway, register the DLT template, implement `send_code`, and set
`SMS_API_KEY`.
"""

from __future__ import annotations

from app.core.config import settings


class SmsSendFailed(RuntimeError):
    pass


def is_configured() -> bool:
    return bool(settings.SMS_API_KEY)


async def send_code(*, to: str, code: str, purpose: str) -> None:
    raise SmsSendFailed(
        "SMS provider not implemented — choose a gateway (MSG91 / Gupshup / Twilio), "
        "register the DLT template, and complete app/integrations/sms.py"
    )

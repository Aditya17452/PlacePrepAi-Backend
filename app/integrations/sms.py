"""SMS via MSG91, behind a flag that stays off until DLT is approved.

In India a transactional SMS needs a DLT-registered template; sending before that
approval is rejected at the carrier, and from our side that rejection is hard to tell
from a delivered message. So `SMS_ENABLED` gates it: until it is turned on, phone OTP
returns 503 and says so rather than silently going nowhere.

To turn on: register the template on DLT, set `MSG91_TEMPLATE_ID`, `MSG91_SENDER_ID` and
`SMS_API_KEY`, then `SMS_ENABLED=true`. The variable in the template must be `otp`.
"""

from __future__ import annotations

import httpx

from app.core.config import settings
from app.core.logging_setup import get_logger

log = get_logger("integrations.sms")

FLOW_ENDPOINT = "https://control.msg91.com/api/v5/flow/"


class SmsSendFailed(RuntimeError):
    pass


class SmsDisabled(SmsSendFailed):
    """The channel is deliberately switched off, not misconfigured."""


def is_configured() -> bool:
    """True only when the flag is on *and* every credential is present.

    Both halves matter: a configured-but-disabled channel must stay silent, and an
    enabled-but-unconfigured one must fail loudly rather than appear to work.
    """
    return bool(
        settings.SMS_ENABLED
        and settings.SMS_API_KEY
        and settings.MSG91_TEMPLATE_ID
    )


def status() -> str:
    """Human-readable state, for the admin config-check endpoint."""
    if not settings.SMS_ENABLED:
        return "disabled (SMS_ENABLED=false, waiting on DLT approval)"
    missing = [
        name
        for name, value in (
            ("SMS_API_KEY", settings.SMS_API_KEY),
            ("MSG91_TEMPLATE_ID", settings.MSG91_TEMPLATE_ID),
        )
        if not value
    ]
    return f"enabled but missing {', '.join(missing)}" if missing else "enabled"


def _normalise(number: str) -> str:
    """MSG91 wants country code and digits only, no '+'."""
    digits = "".join(ch for ch in number if ch.isdigit())
    # A bare 10-digit Indian number is the common case from the signup form.
    if len(digits) == 10:
        digits = f"91{digits}"
    return digits


async def send_code(*, to: str, code: str, purpose: str) -> None:
    if not settings.SMS_ENABLED:
        raise SmsDisabled(
            "SMS is switched off (SMS_ENABLED=false) while the DLT template is pending."
        )
    if not (settings.SMS_API_KEY and settings.MSG91_TEMPLATE_ID):
        raise SmsSendFailed(
            "SMS is enabled but SMS_API_KEY or MSG91_TEMPLATE_ID is not set."
        )

    payload: dict = {
        "template_id": settings.MSG91_TEMPLATE_ID,
        "recipients": [{"mobiles": _normalise(to), "otp": code}],
    }
    if settings.MSG91_SENDER_ID:
        payload["sender"] = settings.MSG91_SENDER_ID

    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.post(
                FLOW_ENDPOINT,
                headers={
                    "authkey": settings.SMS_API_KEY,
                    "Content-Type": "application/json",
                },
                json=payload,
            )
    except httpx.HTTPError as exc:
        log.error("sms_transport_failed", error=str(exc)[:160])
        raise SmsSendFailed("Could not reach the SMS provider.") from exc

    if resp.status_code >= 400:
        # Status only: the body echoes the recipient's number.
        log.error("sms_send_rejected", status=resp.status_code)
        raise SmsSendFailed(f"SMS provider rejected the request ({resp.status_code}).")

    # MSG91 answers 200 with {"type": "error"} for a rejected template or sender, so a
    # 200 alone is not success.
    try:
        body = resp.json()
    except ValueError:
        body = {}
    if isinstance(body, dict) and str(body.get("type", "")).lower() == "error":
        log.error("sms_send_error_body", message=str(body.get("message"))[:160])
        raise SmsSendFailed("SMS provider reported an error for this template.")

    log.info("sms_sent", purpose=purpose)

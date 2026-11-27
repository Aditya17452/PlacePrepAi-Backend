"""Razorpay client and webhook signature verification.

Extracted from the payments router so the payment *rules* (prices come from the `plans`
table, tokens are granted only by the verified webhook, idempotent on `payment_id`) sit in
one place and the vendor protocol in another.

`verify_webhook_signature` compares in constant time over the **raw** request body. Parsing
the JSON first and re-serialising it would change the bytes and break the signature — a
common way to get this wrong.
"""

from __future__ import annotations

import hashlib
import hmac
from typing import Any

from app.core.config import settings
from app.core.logging_setup import get_logger

log = get_logger("integrations.razorpay")


class RazorpayNotConfigured(RuntimeError):
    pass


class RazorpayError(RuntimeError):
    """The provider rejected the request."""


def is_configured() -> bool:
    return bool(settings.RAZORPAY_KEY_ID and settings.RAZORPAY_KEY_SECRET)


def client():  # type: ignore[no-untyped-def]
    if not is_configured():
        raise RazorpayNotConfigured("Razorpay keys are not set")
    import razorpay

    return razorpay.Client(
        auth=(settings.RAZORPAY_KEY_ID, settings.RAZORPAY_KEY_SECRET)
    )


def verify_webhook_signature(raw_body: bytes, signature: str) -> bool:
    """HMAC-SHA256 of the raw body against the webhook secret."""
    if not settings.RAZORPAY_WEBHOOK_SECRET:
        log.error("webhook_secret_missing")
        return False
    expected = hmac.new(
        settings.RAZORPAY_WEBHOOK_SECRET.encode("utf-8"), raw_body, hashlib.sha256
    ).hexdigest()
    return hmac.compare_digest(expected, signature or "")


def create_order(*, amount_paise: int, notes: dict[str, str]) -> dict[str, Any]:
    try:
        return client().order.create(
            {
                "amount": int(amount_paise),
                "currency": "INR",
                "payment_capture": 1,
                "notes": notes,
            }
        )
    except RazorpayNotConfigured:
        raise
    except Exception as exc:
        log.error("order_create_failed", error=str(exc)[:200])
        raise RazorpayError(str(exc)) from exc


def refund(*, payment_id: str, amount_paise: int) -> dict[str, Any]:
    try:
        return client().payment.refund(payment_id, {"amount": int(amount_paise)})
    except RazorpayNotConfigured:
        raise
    except Exception as exc:
        log.error("refund_failed", error=str(exc)[:200])
        raise RazorpayError(str(exc)) from exc


def extract_entity(event: dict[str, Any]) -> dict[str, Any]:
    """Pull the payment or refund entity out of a webhook payload."""
    payload = event.get("payload", {}) or {}
    return (payload.get("payment", {}) or {}).get("entity", {}) or (
        payload.get("refund", {}) or {}
    ).get("entity", {}) or {}

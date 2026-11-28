"""OTP delivery gates: email via Resend, SMS via MSG91 behind a flag.

The point of these tests is that an unavailable channel says so. Silently accepting an
OTP request that was never delivered is the failure mode worth preventing.
"""

from __future__ import annotations

import pytest

from app.core.config import settings
from app.integrations import email as email_provider
from app.integrations import sms as sms_provider
from app.services import notify

# ── SMS: the flag ─────────────────────────────────────────────────────────────


def test_sms_is_not_configured_while_the_flag_is_off(monkeypatch):
    """Credentials present but the flag off must stay off — DLT is not approved yet."""
    monkeypatch.setattr(settings, "SMS_ENABLED", False)
    monkeypatch.setattr(settings, "SMS_API_KEY", "key")
    monkeypatch.setattr(settings, "MSG91_TEMPLATE_ID", "tmpl")
    assert sms_provider.is_configured() is False
    assert "disabled" in sms_provider.status()
    assert "DLT" in sms_provider.status()


def test_sms_enabled_but_unconfigured_reports_what_is_missing(monkeypatch):
    monkeypatch.setattr(settings, "SMS_ENABLED", True)
    monkeypatch.setattr(settings, "SMS_API_KEY", "")
    monkeypatch.setattr(settings, "MSG91_TEMPLATE_ID", "")
    assert sms_provider.is_configured() is False
    status = sms_provider.status()
    assert "SMS_API_KEY" in status and "MSG91_TEMPLATE_ID" in status


def test_sms_configured_when_flag_on_and_credentials_set(monkeypatch):
    monkeypatch.setattr(settings, "SMS_ENABLED", True)
    monkeypatch.setattr(settings, "SMS_API_KEY", "key")
    monkeypatch.setattr(settings, "MSG91_TEMPLATE_ID", "tmpl")
    assert sms_provider.is_configured() is True
    assert sms_provider.status() == "enabled"


@pytest.mark.asyncio
async def test_sending_sms_while_disabled_raises_rather_than_no_opping(monkeypatch):
    monkeypatch.setattr(settings, "SMS_ENABLED", False)
    with pytest.raises(sms_provider.SmsDisabled):
        await sms_provider.send_code(to="9876543210", code="123456", purpose="phone_otp")


@pytest.mark.parametrize(
    ("given", "expected"),
    [
        ("9876543210", "919876543210"),
        ("+91 98765 43210", "919876543210"),
        ("919876543210", "919876543210"),
    ],
)
def test_indian_numbers_get_a_country_code(given, expected):
    assert sms_provider._normalise(given) == expected


# ── notify: the policy layer ──────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_sms_in_production_without_a_provider_is_an_explicit_failure(monkeypatch):
    monkeypatch.setattr(settings, "ENV", "production")
    monkeypatch.setattr(settings, "SMS_ENABLED", False)
    with pytest.raises(notify.NotificationUnavailable) as exc:
        await notify.send_sms_code(to="9876543210", code="123456", purpose="phone_otp")
    # The message distinguishes "switched off" from "misconfigured".
    assert "disabled" in str(exc.value)


@pytest.mark.asyncio
async def test_email_in_production_without_a_provider_is_an_explicit_failure(monkeypatch):
    monkeypatch.setattr(settings, "ENV", "production")
    monkeypatch.setattr(settings, "RESEND_API_KEY", "")
    with pytest.raises(notify.NotificationUnavailable):
        await notify.send_email_code(
            to="someone@example.com", code="123456", purpose="email_otp"
        )


@pytest.mark.asyncio
async def test_codes_go_to_the_log_locally_so_dev_works_without_an_account(monkeypatch):
    monkeypatch.setattr(settings, "ENV", "local")
    monkeypatch.setattr(settings, "RESEND_API_KEY", "")
    monkeypatch.setattr(settings, "SMS_ENABLED", False)
    # Neither raises: local development must not need a provider account.
    await notify.send_email_code(to="a@example.com", code="123456", purpose="email_otp")
    await notify.send_sms_code(to="9876543210", code="123456", purpose="phone_otp")


def test_email_configuration_follows_the_key(monkeypatch):
    monkeypatch.setattr(settings, "RESEND_API_KEY", "")
    assert email_provider.is_configured() is False
    monkeypatch.setattr(settings, "RESEND_API_KEY", "re_test")
    assert email_provider.is_configured() is True

"""SSRF guard, config validation, proctoring scoring, and ownership checks."""

from __future__ import annotations

import pytest

from app.services.candidate_links import (
    UnsafeUrl,
    assert_safe,
    classify,
    extract_urls,
)

# ── SSRF ──────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "url",
    [
        "http://169.254.169.254/latest/meta-data/",  # cloud metadata
        "https://169.254.169.254/latest/meta-data/",
        "http://localhost:8000/api/super/stats",
        "https://127.0.0.1/",
        "https://10.0.0.5/internal",
        "file:///etc/passwd",
        "https://evil.example.com/webhook",
        "https://github.com.evil.example.com/user",  # lookalike host
    ],
)
def test_unsafe_urls_are_refused(url):
    with pytest.raises(UnsafeUrl):
        assert_safe(url)


def test_http_is_refused_even_for_an_allowed_host():
    with pytest.raises(UnsafeUrl):
        assert_safe("http://github.com/someone")


def test_allowlisted_host_passes():
    # Hits DNS; github.com resolving to a public address is the thing being asserted.
    assert assert_safe("https://github.com/torvalds") == "github.com"


def test_linkedin_is_classified_but_never_fetched():
    assert classify("https://www.linkedin.com/in/someone") == "linkedin"
    with pytest.raises(UnsafeUrl):
        assert_safe("https://www.linkedin.com/in/someone")


def test_extract_urls_deduplicates_and_trims_punctuation():
    text = (
        "See https://github.com/me, and https://github.com/me again. "
        "Portfolio: https://example.com/page)"
    )
    urls = extract_urls(text)
    assert urls == ["https://github.com/me", "https://example.com/page"]


# ── Configuration validation ──────────────────────────────────────────────────


def test_production_refuses_a_placeholder_jwt_secret(monkeypatch):
    from app.core.config import Settings

    monkeypatch.setenv("ENV", "production")
    monkeypatch.setenv("JWT_SECRET", "secret")
    monkeypatch.setenv("DATABASE_URL", "postgresql://u:p@host/db")
    with pytest.raises(ValueError, match="JWT_SECRET"):
        Settings(_env_file=None)


def test_production_refuses_sqlite(monkeypatch):
    from app.core.config import Settings

    monkeypatch.setenv("ENV", "production")
    monkeypatch.setenv("JWT_SECRET", "x" * 40)
    monkeypatch.setenv("DATABASE_URL", "sqlite:///./placeprep.db")
    with pytest.raises(ValueError, match="SQLite"):
        Settings(_env_file=None)


def test_wildcard_cors_is_refused(monkeypatch):
    from app.core.config import Settings

    monkeypatch.setenv("CORS_ORIGINS", "*")
    monkeypatch.setenv("JWT_SECRET", "x" * 40)
    monkeypatch.setenv("DATABASE_URL", "sqlite:///./x.db")
    with pytest.raises(ValueError, match="CORS_ORIGINS"):
        Settings(_env_file=None)


def test_production_refuses_a_razorpay_test_key(monkeypatch):
    from app.core.config import Settings

    monkeypatch.setenv("ENV", "production")
    monkeypatch.setenv("JWT_SECRET", "x" * 40)
    monkeypatch.setenv("DATABASE_URL", "postgresql://u:p@host/db")
    monkeypatch.setenv("CORS_ORIGINS", "https://www.placeprepai.fun")
    monkeypatch.setenv("RAZORPAY_KEY_ID", "rzp_test_abc")
    monkeypatch.setenv("RAZORPAY_WEBHOOK_SECRET", "whsec")
    monkeypatch.setenv("UPSTASH_REDIS_REST_URL", "https://x.upstash.io")
    monkeypatch.setenv("UPSTASH_REDIS_REST_TOKEN", "tok")
    monkeypatch.setenv("GROQ_API_KEY", "gsk_x")
    monkeypatch.setenv("ADMIN_BOOTSTRAP_EMAILS", "a@b.com")
    monkeypatch.setenv("SUPABASE_SERVICE_ROLE_KEY", "svc")
    with pytest.raises(ValueError, match="test key"):
        Settings(_env_file=None)


# ── Proctoring ────────────────────────────────────────────────────────────────














# ── Ownership ─────────────────────────────────────────────────────────────────








# ── Health ────────────────────────────────────────────────────────────────────





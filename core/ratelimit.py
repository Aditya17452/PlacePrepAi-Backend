"""Rate limiting, per user and per IP.

A fixed-window counter in Redis. Deliberately not an in-process limiter: on Vercel every
request may land in a different function instance, so anything held in memory counts
almost nothing.

Limits are configured as `"<count>/<window_seconds>"` strings in settings, so they can be
tuned per environment without a code change.
"""

from __future__ import annotations

from dataclasses import dataclass

from fastapi import HTTPException, Request, status

from core import kv
from core.config import settings
from core.logging_setup import get_logger

log = get_logger("ratelimit")


@dataclass(frozen=True)
class Limit:
    count: int
    window_sec: int

    @classmethod
    def parse(cls, spec: str) -> Limit:
        count, _, window = spec.partition("/")
        return cls(count=int(count), window_sec=int(window))


def client_ip(request: Request) -> str:
    """Best-effort client IP.

    Vercel sets `x-forwarded-for`; the left-most entry is the client. We only use this for
    rate limiting, never for authorisation, because it is spoofable upstream of us.
    """
    forwarded = request.headers.get("x-forwarded-for", "")
    if forwarded:
        return forwarded.split(",")[0].strip()
    real = request.headers.get("x-real-ip")
    if real:
        return real.strip()
    return request.client.host if request.client else "unknown"


async def enforce(
    *,
    bucket: str,
    identifier: str,
    spec: str,
) -> None:
    """Count one hit and raise 429 when the window limit is exceeded."""
    limit = Limit.parse(spec)
    key = f"rl:{bucket}:{identifier}:{limit.window_sec}"
    try:
        hits = await kv.incr_with_ttl(key, limit.window_sec)
    except Exception as exc:
        # A limiter outage must not take the API down with it. Log loudly and allow.
        log.warning("ratelimit_backend_unavailable", bucket=bucket, error=str(exc))
        return

    if hits > limit.count:
        log.info("ratelimit_exceeded", bucket=bucket, identifier=identifier, hits=hits)
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail="Too many requests. Please wait a moment and try again.",
            headers={"Retry-After": str(limit.window_sec)},
        )


async def limit_by_ip(request: Request, bucket: str, spec: str) -> None:
    await enforce(bucket=f"{bucket}:ip", identifier=client_ip(request), spec=spec)


async def limit_by_user(user_id: str, bucket: str, spec: str) -> None:
    await enforce(bucket=f"{bucket}:user", identifier=user_id, spec=spec)


async def limit_auth(request: Request) -> None:
    await limit_by_ip(request, "auth", settings.RATE_LIMIT_AUTH)


async def limit_otp(request: Request, destination: str) -> None:
    """OTP sends are limited per destination *and* per IP.

    Per-destination stops someone spamming one person's phone; per-IP stops someone
    walking a list of numbers.
    """
    await enforce(bucket="otp:dest", identifier=destination, spec=settings.RATE_LIMIT_OTP)
    await limit_by_ip(request, "otp", settings.RATE_LIMIT_OTP)


async def limit_upload(request: Request, user_id: str) -> None:
    await limit_by_user(user_id, "upload", settings.RATE_LIMIT_UPLOAD)
    await limit_by_ip(request, "upload", settings.RATE_LIMIT_UPLOAD)


async def limit_interview_start(request: Request, user_id: str) -> None:
    await limit_by_user(user_id, "interview_start", settings.RATE_LIMIT_INTERVIEW_START)
    await limit_by_ip(request, "interview_start", settings.RATE_LIMIT_INTERVIEW_START)


async def limit_llm(user_id: str) -> None:
    await limit_by_user(user_id, "llm", settings.RATE_LIMIT_LLM)

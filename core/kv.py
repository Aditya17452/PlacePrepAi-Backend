"""Key-value store for live session state and rate limiting.

Three backends behind one tiny interface:

* **Upstash REST** (production on Vercel). Plain HTTPS, so there is no connection pool to
  keep warm across short-lived function invocations.
* **redis-py over TCP** (local development, or any always-on host).
* **In-process dict** (tests, and local dev with nothing configured). Explicitly *not*
  allowed in production — `core.config` already refuses to start without Redis there,
  because in-process state is exactly the bug this replaces.

Everything is async. Values are JSON.
"""

from __future__ import annotations

import json
import time
from typing import Any

import httpx

from core.config import settings


class KVUnavailable(RuntimeError):
    """Raised when the configured backend cannot be reached."""


class _Backend:
    async def get(self, key: str) -> str | None: ...
    async def set(self, key: str, value: str, ttl_sec: int | None = None) -> None: ...
    async def delete(self, key: str) -> None: ...
    async def incr_with_ttl(self, key: str, ttl_sec: int) -> int: ...
    async def ping(self) -> bool: ...


class _MemoryBackend(_Backend):
    """Single-process fallback. Fine for tests, useless behind more than one worker."""

    def __init__(self) -> None:
        self._data: dict[str, tuple[str, float | None]] = {}

    def _expired(self, key: str) -> bool:
        item = self._data.get(key)
        if item is None:
            return True
        _, expires = item
        if expires is not None and expires <= time.time():
            self._data.pop(key, None)
            return True
        return False

    async def get(self, key: str) -> str | None:
        if self._expired(key):
            return None
        return self._data[key][0]

    async def set(self, key: str, value: str, ttl_sec: int | None = None) -> None:
        expires = time.time() + ttl_sec if ttl_sec else None
        self._data[key] = (value, expires)

    async def delete(self, key: str) -> None:
        self._data.pop(key, None)

    async def incr_with_ttl(self, key: str, ttl_sec: int) -> int:
        current = 0 if self._expired(key) else int(self._data[key][0])
        expires = (
            self._data[key][1]
            if key in self._data and self._data[key][1]
            else time.time() + ttl_sec
        )
        self._data[key] = (str(current + 1), expires)
        return current + 1

    async def ping(self) -> bool:
        return True


class _UpstashRestBackend(_Backend):
    """Upstash's REST API. Commands are sent as a path or a JSON array."""

    def __init__(self, url: str, token: str) -> None:
        self._url = url.rstrip("/")
        self._headers = {"Authorization": f"Bearer {token}"}

    async def _command(self, *parts: str | int) -> Any:
        payload = [str(p) for p in parts]
        try:
            async with httpx.AsyncClient(timeout=5.0) as client:
                resp = await client.post(
                    self._url, headers=self._headers, json=payload
                )
                resp.raise_for_status()
                return resp.json().get("result")
        except httpx.HTTPError as exc:
            raise KVUnavailable(f"Upstash request failed: {exc}") from exc

    async def get(self, key: str) -> str | None:
        result = await self._command("GET", key)
        return None if result is None else str(result)

    async def set(self, key: str, value: str, ttl_sec: int | None = None) -> None:
        if ttl_sec:
            await self._command("SET", key, value, "EX", ttl_sec)
        else:
            await self._command("SET", key, value)

    async def delete(self, key: str) -> None:
        await self._command("DEL", key)

    async def incr_with_ttl(self, key: str, ttl_sec: int) -> int:
        count = int(await self._command("INCR", key) or 0)
        if count == 1:
            # First hit in this window sets the expiry; later hits must not extend it,
            # or a steady stream of requests would keep the window open forever.
            await self._command("EXPIRE", key, ttl_sec)
        return count

    async def ping(self) -> bool:
        return (await self._command("PING")) is not None


class _RedisTcpBackend(_Backend):
    def __init__(self, url: str) -> None:
        import redis.asyncio as aioredis  # imported lazily: optional in some envs

        self._client = aioredis.from_url(url, decode_responses=True)

    async def get(self, key: str) -> str | None:
        return await self._client.get(key)

    async def set(self, key: str, value: str, ttl_sec: int | None = None) -> None:
        await self._client.set(key, value, ex=ttl_sec)

    async def delete(self, key: str) -> None:
        await self._client.delete(key)

    async def incr_with_ttl(self, key: str, ttl_sec: int) -> int:
        async with self._client.pipeline(transaction=True) as pipe:
            pipe.incr(key)
            pipe.ttl(key)
            count, ttl = await pipe.execute()
        if ttl is None or ttl < 0:
            await self._client.expire(key, ttl_sec)
        return int(count)

    async def ping(self) -> bool:
        return bool(await self._client.ping())


def _build_backend() -> _Backend:
    if settings.UPSTASH_REDIS_REST_URL and settings.UPSTASH_REDIS_REST_TOKEN:
        return _UpstashRestBackend(
            settings.UPSTASH_REDIS_REST_URL, settings.UPSTASH_REDIS_REST_TOKEN
        )
    if settings.REDIS_URL:
        return _RedisTcpBackend(settings.REDIS_URL)
    if settings.is_production:  # pragma: no cover - config validation prevents this
        raise KVUnavailable("No Redis configured in production")
    return _MemoryBackend()


_backend: _Backend | None = None


def backend() -> _Backend:
    global _backend
    if _backend is None:
        _backend = _build_backend()
    return _backend


def reset_backend_for_tests(new: _Backend | None = None) -> None:
    global _backend
    _backend = new


# ── JSON helpers used by callers ──────────────────────────────────────────────


async def get_json(key: str) -> Any | None:
    raw = await backend().get(key)
    if raw is None:
        return None
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return None


async def set_json(key: str, value: Any, ttl_sec: int | None = None) -> None:
    await backend().set(key, json.dumps(value, default=str), ttl_sec)


async def delete(key: str) -> None:
    await backend().delete(key)


async def incr_with_ttl(key: str, ttl_sec: int) -> int:
    return await backend().incr_with_ttl(key, ttl_sec)


async def healthy() -> bool:
    try:
        return await backend().ping()
    except Exception:
        return False


def is_memory_backend() -> bool:
    return isinstance(backend(), _MemoryBackend)

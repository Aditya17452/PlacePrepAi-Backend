"""Private file storage on Supabase Storage.

Resumes used to be read into memory and the text written to a column; the file itself was
never kept, and anything written to local disk on a serverless host disappears with the
function anyway. Files now go to a **private** bucket, and reads are short-lived signed
URLs, so an object key leaking is not the same as the file leaking.

Uploads are best-effort: if storage is not configured the caller still gets the extracted
text, because losing the original file is better than losing the upload.
"""

from __future__ import annotations

import httpx

from core.config import settings
from core.logging_setup import get_logger

log = get_logger("storage")


class StorageUnavailable(RuntimeError):
    pass


def _require_config() -> tuple[str, str]:
    if not (settings.SUPABASE_URL and settings.SUPABASE_SERVICE_ROLE_KEY):
        raise StorageUnavailable("Supabase Storage is not configured")
    return settings.SUPABASE_URL.rstrip("/"), settings.SUPABASE_SERVICE_ROLE_KEY


def _headers(key: str, content_type: str | None = None) -> dict[str, str]:
    headers = {
        "Authorization": f"Bearer {key}",
        "apikey": key,
    }
    if content_type:
        headers["Content-Type"] = content_type
    return headers


async def upload(
    *, bucket: str, path: str, data: bytes, content_type: str, upsert: bool = True
) -> str:
    """Upload bytes and return the object path. Raises `StorageUnavailable` on failure."""
    base, key = _require_config()
    url = f"{base}/storage/v1/object/{bucket}/{path}"
    headers = _headers(key, content_type)
    if upsert:
        headers["x-upsert"] = "true"

    async with httpx.AsyncClient(timeout=20.0) as client:
        resp = await client.post(url, headers=headers, content=data)
        if resp.status_code >= 400:
            log.error(
                "storage_upload_failed", status=resp.status_code, bucket=bucket
            )
            raise StorageUnavailable(f"Upload failed with {resp.status_code}")
    return path


async def signed_url(*, bucket: str, path: str, expires_in_sec: int = 300) -> str:
    """A short-lived read URL for a private object."""
    base, key = _require_config()
    url = f"{base}/storage/v1/object/sign/{bucket}/{path}"

    async with httpx.AsyncClient(timeout=10.0) as client:
        resp = await client.post(
            url, headers=_headers(key, "application/json"), json={"expiresIn": expires_in_sec}
        )
        if resp.status_code >= 400:
            raise StorageUnavailable(f"Could not sign URL ({resp.status_code})")
        signed = resp.json().get("signedURL") or resp.json().get("signedUrl")
    if not signed:
        raise StorageUnavailable("Storage did not return a signed URL")
    return f"{base}/storage/v1{signed}" if signed.startswith("/") else signed


async def delete(*, bucket: str, paths: list[str]) -> int:
    """Remove objects. Used by the 14-day snapshot purge."""
    base, key = _require_config()
    url = f"{base}/storage/v1/object/{bucket}"

    async with httpx.AsyncClient(timeout=20.0) as client:
        resp = await client.request(
            "DELETE",
            url,
            headers=_headers(key, "application/json"),
            json={"prefixes": paths},
        )
        if resp.status_code >= 400:
            log.error("storage_delete_failed", status=resp.status_code)
            return 0
    return len(paths)


def is_configured() -> bool:
    return bool(settings.SUPABASE_URL and settings.SUPABASE_SERVICE_ROLE_KEY)

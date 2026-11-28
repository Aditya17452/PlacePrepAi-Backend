"""Fetching the public profiles a candidate links from their resume.

Two problems with the previous version:

1. **SSRF.** It fetched any URL found in an uploaded PDF, following redirects, with no
   scheme, host or IP filtering. On a cloud host that reaches the instance metadata
   endpoint and anything else on the private network — an attacker only had to upload a
   resume containing a link.
2. **Blocking I/O in an async handler.** It used `requests` plus `time.sleep(0.3)` inside
   `async def upload_resume`, so up to eight sequential 8-second fetches stalled the whole
   event loop for every other user.

Now: an allowlist of hosts, DNS resolution with private-range rejection, no cross-host
redirects, a byte cap, `httpx.AsyncClient` throughout, and a server token for GitHub so
the rate limit is per-account (5,000/hour) rather than 60/hour shared by every user of the
instance.

LinkedIn is **not** fetched. Scraping it breaks their terms and is blocked anyway; the
student uploads their profile PDF instead (PLAN.md Phase 2).
"""

from __future__ import annotations

import ipaddress
import re
import socket
from dataclasses import dataclass, field
from urllib.parse import urlparse

import httpx

from app.core.config import settings
from app.core.logging_setup import get_logger
from app.integrations import github, leetcode

log = get_logger("scraper")

#: Only these hosts are ever fetched. A portfolio link is read from the resume text but
#: not retrieved, because an arbitrary URL is the whole SSRF problem.
_ALLOWED_HOSTS = {
    "github.com": "github",
    "www.github.com": "github",
    "api.github.com": "github",
    "leetcode.com": "leetcode",
    "www.leetcode.com": "leetcode",
}

_URL_PATTERN = re.compile(r"https?://[^\s\)\]\},\"'<>]+")

#: Resumes almost never write the scheme — they print "github.com/you" under the name,
#: and PDF extraction yields exactly that. Requiring https:// meant a resume with a
#: GitHub and a LinkedIn link produced "No links found in resume.", so the interviewer
#: had nothing to ask about even when the candidate pointed at their profile.
#: Restricted to the hosts we would fetch anyway, so this widens extraction, not reach:
#: classify() and assert_safe() still gate every request.
_BARE_URL_PATTERN = re.compile(
    r"(?<![@\w.])(?:www\.)?((?:github|linkedin|leetcode)\.com/[A-Za-z0-9._~%/-]+)",
    re.IGNORECASE,
)

_MAX_URLS = 8


class UnsafeUrl(ValueError):
    """The URL is not on the allowlist, or resolves somewhere it should not."""


@dataclass
class ScrapeResult:
    urls_found: int = 0
    urls_fetched: int = 0
    github: list[dict] = field(default_factory=list)
    leetcode: list[dict] = field(default_factory=list)
    #: Links we deliberately did not fetch, kept as references for the student to confirm.
    references: list[dict] = field(default_factory=list)
    summary: str = ""


# ── URL handling ──────────────────────────────────────────────────────────────


def extract_urls(text: str) -> list[str]:
    seen: set[str] = set()
    found: list[str] = []

    def add(url: str) -> None:
        url = url.rstrip(".,;:)]}\"'")
        key = url.lower().replace("http://", "https://")
        if key not in seen:
            seen.add(key)
            found.append(url)

    for raw in _URL_PATTERN.findall(text or ""):
        add(raw)
    # Scheme-less profile links, normalised so the rest of the pipeline sees a URL.
    for host_and_path in _BARE_URL_PATTERN.findall(text or ""):
        add(f"https://{host_and_path}")
    return found


def classify(url: str) -> str:
    host = (urlparse(url).hostname or "").lower()
    if host in _ALLOWED_HOSTS:
        return _ALLOWED_HOSTS[host]
    if host.endswith("linkedin.com"):
        return "linkedin"
    return "website"


def assert_safe(url: str) -> str:
    """Validate a URL before fetching it. Returns the hostname.

    Checks, in order: HTTPS only, host on the allowlist, and every resolved address is
    globally routable. The DNS check is what stops a hostname that an attacker controls
    from pointing at 169.254.169.254 or 10.x.
    """
    parsed = urlparse(url)
    if parsed.scheme != "https":
        raise UnsafeUrl(f"Only https is fetched, got {parsed.scheme!r}")

    host = (parsed.hostname or "").lower()
    if host not in _ALLOWED_HOSTS:
        raise UnsafeUrl(f"Host {host!r} is not on the fetch allowlist")

    try:
        infos = socket.getaddrinfo(host, 443, proto=socket.IPPROTO_TCP)
    except socket.gaierror as exc:
        raise UnsafeUrl(f"Could not resolve {host!r}") from exc

    for info in infos:
        address = ipaddress.ip_address(info[4][0])
        if not address.is_global or address.is_multicast:
            raise UnsafeUrl(f"{host!r} resolves to a non-public address ({address})")

    return host


# ── Orchestration ─────────────────────────────────────────────────────────────


async def scrape_resume_links(resume_text: str) -> ScrapeResult:
    """Fetch the allowlisted profiles linked from a resume. Never raises."""
    urls = extract_urls(resume_text)
    result = ScrapeResult(urls_found=len(urls))
    if not urls:
        result.summary = ""
        return result

    async with httpx.AsyncClient(
        timeout=settings.SCRAPE_TIMEOUT_SEC,
        follow_redirects=False,  # a redirect could leave the allowlist
        limits=httpx.Limits(max_connections=4),
    ) as client:
        for url in urls[:_MAX_URLS]:
            kind = classify(url)

            if kind in ("linkedin", "website"):
                result.references.append({"kind": kind, "url": url})
                continue

            try:
                assert_safe(url)
            except UnsafeUrl as exc:
                log.info("url_rejected", url=url[:120], reason=str(exc))
                result.references.append({"kind": kind, "url": url, "skipped": True})
                continue

            try:
                if kind == "github":
                    handle = _github_handle(url)
                    if not handle:
                        continue
                    data = await github.fetch_profile(client, handle)
                    result.github.append(data)
                elif kind == "leetcode":
                    handle = _leetcode_handle(url)
                    if not handle:
                        continue
                    data = await leetcode.fetch_stats(client, handle)
                    result.leetcode.append(data)
                result.urls_fetched += 1
            except httpx.HTTPError as exc:
                log.info("fetch_failed", kind=kind, error=str(exc)[:120])

    result.summary = build_summary(result)
    return result


def _github_handle(url: str) -> str:
    parts = [p for p in urlparse(url).path.split("/") if p]
    return parts[0] if parts else ""


def _leetcode_handle(url: str) -> str:
    parts = [p for p in urlparse(url).path.split("/") if p]
    if not parts:
        return ""
    handle = parts[-1]
    return "" if handle in ("problemset", "contest", "problems", "u") else handle


def build_summary(result: ScrapeResult) -> str:
    """A compact text summary for the interview prompt."""
    parts: list[str] = []

    for profile in result.github:
        if profile.get("error"):
            continue
        repos = profile.get("repos", [])
        names = ", ".join(
            f"{r['name']} ({r.get('language', '?')}, {r.get('stars', 0)}★)"
            for r in repos[:4]
        )
        parts.append(
            f"GitHub @{profile.get('username')}: {profile.get('public_repos', 0)} public "
            f"repos. Top projects: {names}. Bio: {profile.get('bio') or 'n/a'}."
        )
        for repo in repos[:2]:
            snippet = repo.get("readme_snippet")
            if snippet:
                parts.append(f"README of '{repo['name']}': {snippet[:300]}")

    for profile in result.leetcode:
        if profile.get("error"):
            continue
        parts.append(
            f"LeetCode @{profile.get('username')}: {profile.get('total_solved', 0)} "
            f"solved (E{profile.get('easy_solved', 0)}/"
            f"M{profile.get('medium_solved', 0)}/H{profile.get('hard_solved', 0)}), "
            f"rank {profile.get('ranking') or 'n/a'}."
        )

    return "\n".join(parts)

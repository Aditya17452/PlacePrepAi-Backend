"""GitHub REST API client.

A server token raises the limit from 60 requests/hour **per instance IP — shared by every
user** to 5,000/hour for the account, which is why the unauthenticated version fell over
as soon as more than a handful of students uploaded resumes.

The User-Agent is a generic client name. It never carries the user's details.
"""

from __future__ import annotations

import asyncio
import re

import httpx

from app.core.config import settings
from app.core.logging_setup import get_logger

log = get_logger("integrations.github")

API = "https://api.github.com"


def headers(raw: bool = False) -> dict[str, str]:
    value = {
        "Accept": "application/vnd.github.raw" if raw else "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
        "User-Agent": "placeprep-ai",
    }
    if settings.GITHUB_TOKEN:
        value["Authorization"] = f"Bearer {settings.GITHUB_TOKEN}"
    return value


async def fetch_profile(client: httpx.AsyncClient, username: str) -> dict:
    """Profile plus top repositories, with README snippets for the top few.

    Sorted by stars then recency: "most recently pushed" alone surfaces throwaway repos,
    which then became the basis of the interview questions.
    """
    user_resp = await client.get(f"{API}/users/{username}", headers=headers())
    if user_resp.status_code == 404:
        return {"type": "github", "username": username, "error": "not_found"}
    if user_resp.status_code == 403:
        log.warning("rate_limited", has_token=bool(settings.GITHUB_TOKEN))
        return {"type": "github", "username": username, "error": "rate_limited"}
    user_resp.raise_for_status()
    user = user_resp.json()

    repos_resp = await client.get(
        f"{API}/users/{username}/repos",
        params={"sort": "updated", "per_page": 30},
        headers=headers(),
    )
    repos = repos_resp.json() if repos_resp.status_code == 200 else []
    if not isinstance(repos, list):
        repos = []

    own = [r for r in repos if isinstance(r, dict) and not r.get("fork")]
    own.sort(
        key=lambda r: (r.get("stargazers_count", 0), r.get("pushed_at") or ""),
        reverse=True,
    )
    top = own[:6]

    readmes = await asyncio.gather(
        *(fetch_readme(client, username, r.get("name", "")) for r in top[:3]),
        return_exceptions=True,
    )

    summaries = []
    for index, repo in enumerate(top):
        entry = {
            "name": repo.get("name", ""),
            "description": repo.get("description") or "",
            "language": repo.get("language") or "unknown",
            "stars": repo.get("stargazers_count", 0),
            "topics": repo.get("topics", []),
            "pushed_at": repo.get("pushed_at"),
            "url": repo.get("html_url"),
        }
        if index < len(readmes) and isinstance(readmes[index], str):
            entry["readme_snippet"] = readmes[index][:600]
        summaries.append(entry)

    return {
        "type": "github",
        "username": username,
        "name": user.get("name") or username,
        "bio": user.get("bio") or "",
        "public_repos": user.get("public_repos", 0),
        "followers": user.get("followers", 0),
        "repos": summaries,
    }


async def fetch_readme(client: httpx.AsyncClient, username: str, repo: str) -> str:
    if not repo:
        return ""
    try:
        resp = await client.get(
            f"{API}/repos/{username}/{repo}/readme", headers=headers(raw=True)
        )
        if resp.status_code != 200:
            return ""
        return strip_markdown(resp.text)
    except httpx.HTTPError:
        return ""


def strip_markdown(text: str) -> str:
    """Flatten a README to prose, so a prompt is not mostly syntax."""
    text = re.sub(r"```[\s\S]*?```", "", text)
    text = re.sub(r"!\[[^\]]*\]\([^)]*\)", "", text)
    text = re.sub(r"\[([^\]]+)\]\([^)]+\)", r"\1", text)
    text = re.sub(r"#{1,6}\s+", "", text)
    text = re.sub(r"\*{1,2}([^*]+)\*{1,2}", r"\1", text)
    return re.sub(r"\n{3,}", "\n\n", text).strip()

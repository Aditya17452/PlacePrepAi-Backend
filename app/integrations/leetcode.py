"""LeetCode public profile stats, via their GraphQL endpoint.

Unofficial and unsupported, so every failure path returns a dict with an `error` key
rather than raising: a candidate's LeetCode being unavailable must never stop a resume
upload.
"""

from __future__ import annotations

import httpx

from app.core.logging_setup import get_logger

log = get_logger("integrations.leetcode")

ENDPOINT = "https://leetcode.com/graphql"

QUERY = """
query getUserProfile($username: String!) {
  matchedUser(username: $username) {
    username
    submitStats: submitStatsGlobal {
      acSubmissionNum { difficulty count }
    }
    profile { ranking reputation starRating }
  }
}
"""


async def fetch_stats(client: httpx.AsyncClient, username: str) -> dict:
    try:
        resp = await client.post(
            ENDPOINT,
            json={"query": QUERY, "variables": {"username": username}},
            headers={
                "Content-Type": "application/json",
                "Referer": "https://leetcode.com",
                "User-Agent": "placeprep-ai",
            },
        )
        if resp.status_code != 200:
            return {"type": "leetcode", "username": username, "error": "unavailable"}

        user = (resp.json().get("data") or {}).get("matchedUser")
        if not user:
            return {"type": "leetcode", "username": username, "error": "not_found"}

        stats = (user.get("submitStats") or {}).get("acSubmissionNum") or []
        solved = {s["difficulty"]: s["count"] for s in stats}
        return {
            "type": "leetcode",
            "username": username,
            "ranking": (user.get("profile") or {}).get("ranking"),
            "easy_solved": solved.get("Easy", 0),
            "medium_solved": solved.get("Medium", 0),
            "hard_solved": solved.get("Hard", 0),
            "total_solved": solved.get("All", 0),
        }
    except httpx.HTTPError as exc:
        log.info("fetch_failed", error=str(exc)[:120])
        return {"type": "leetcode", "username": username, "error": "unavailable"}

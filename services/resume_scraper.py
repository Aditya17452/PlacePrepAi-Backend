# backend/services/resume_scraper.py
# Install: pip install requests beautifulsoup4 lxml
# No API keys needed for GitHub public repos

import re
import requests
from bs4 import BeautifulSoup
from urllib.parse import urlparse
import time

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
}

# ── Extract all URLs from resume text ──────────────────────────────────────

def extract_urls(text: str) -> list[str]:
    """Find all URLs in resume text."""
    pattern = r'https?://[^\s\)\]\},\"\'<>]+'
    urls = re.findall(pattern, text)
    # Clean trailing punctuation
    urls = [u.rstrip(".,;:)]}\"'") for u in urls]
    # Deduplicate
    seen = set()
    result = []
    for u in urls:
        if u not in seen:
            seen.add(u)
            result.append(u)
    return result

def classify_url(url: str) -> str:
    """Classify URL type."""
    domain = urlparse(url).netloc.lower()
    if "github.com" in domain:     return "github"
    if "linkedin.com" in domain:   return "linkedin"
    if "leetcode.com" in domain:   return "leetcode"
    if "codeforces.com" in domain: return "codeforces"
    return "website"

# ── GitHub scraper ──────────────────────────────────────────────────────────

def scrape_github(url: str) -> dict:
    """
    Scrapes GitHub profile or repo using public GitHub API.
    No API key needed for public data (60 req/hour unauthenticated).
    """
    parsed = urlparse(url)
    parts = parsed.path.strip("/").split("/")

    if len(parts) == 0 or not parts[0]:
        return {"type": "github", "error": "Invalid GitHub URL"}

    username = parts[0]
    repo_name = parts[1] if len(parts) > 1 else None

    try:
        if repo_name:
            return _scrape_github_repo(username, repo_name)
        else:
            return _scrape_github_profile(username)
    except Exception as e:
        return {"type": "github", "error": str(e), "username": username}

def _scrape_github_profile(username: str) -> dict:
    """Get user's top repos and profile info."""
    # User info
    user_res = requests.get(f"https://api.github.com/users/{username}", headers=HEADERS, timeout=8)
    user_data = user_res.json() if user_res.ok else {}

    # Top repos
    repos_res = requests.get(
        f"https://api.github.com/users/{username}/repos",
        params={"sort": "updated", "per_page": 6},
        headers=HEADERS, timeout=8
    )
    repos = repos_res.json() if repos_res.ok else []

    repo_summaries = []
    for repo in repos:
        if isinstance(repo, dict) and not repo.get("fork"):
            summary = {
                "name": repo.get("name", ""),
                "description": repo.get("description", "No description"),
                "language": repo.get("language", "Unknown"),
                "stars": repo.get("stargazers_count", 0),
                "topics": repo.get("topics", []),
            }
            # Try to get README for top 3 repos
            if len(repo_summaries) < 3:
                readme = _get_readme(username, repo.get("name", ""))
                if readme:
                    summary["readme_snippet"] = readme[:400]
            repo_summaries.append(summary)

    return {
        "type": "github",
        "username": username,
        "name": user_data.get("name", username),
        "bio": user_data.get("bio", ""),
        "public_repos": user_data.get("public_repos", 0),
        "followers": user_data.get("followers", 0),
        "repos": repo_summaries,
    }

def _scrape_github_repo(username: str, repo_name: str) -> dict:
    """Get specific repo details."""
    repo_res = requests.get(
        f"https://api.github.com/repos/{username}/{repo_name}",
        headers=HEADERS, timeout=8
    )
    repo = repo_res.json() if repo_res.ok else {}

    readme = _get_readme(username, repo_name)

    return {
        "type": "github_repo",
        "name": repo.get("name", repo_name),
        "description": repo.get("description", ""),
        "language": repo.get("language", ""),
        "stars": repo.get("stargazers_count", 0),
        "topics": repo.get("topics", []),
        "readme_snippet": readme[:600] if readme else "",
    }

def _get_readme(username: str, repo_name: str) -> str:
    """Fetch README content."""
    try:
        res = requests.get(
            f"https://api.github.com/repos/{username}/{repo_name}/readme",
            headers={**HEADERS, "Accept": "application/vnd.github.raw"},
            timeout=6
        )
        if res.ok:
            text = res.text
            # Strip markdown formatting for cleaner text
            text = re.sub(r'#{1,6}\s+', '', text)
            text = re.sub(r'\*{1,2}([^*]+)\*{1,2}', r'\1', text)
            text = re.sub(r'\[([^\]]+)\]\([^\)]+\)', r'\1', text)
            text = re.sub(r'```[\s\S]*?```', '', text)
            text = re.sub(r'\n{3,}', '\n\n', text).strip()
            return text
    except:
        pass
    return ""

# ── LeetCode scraper ────────────────────────────────────────────────────────

def scrape_leetcode(url: str) -> dict:
    """Scrape LeetCode public profile stats."""
    parsed = urlparse(url)
    parts = parsed.path.strip("/").split("/")
    username = parts[-1] if parts else ""
    if not username or username in ["problemset", "contest"]:
        return {"type": "leetcode", "error": "Not a profile URL"}

    try:
        # LeetCode GraphQL API
        query = """
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
        res = requests.post(
            "https://leetcode.com/graphql",
            json={"query": query, "variables": {"username": username}},
            headers={**HEADERS, "Content-Type": "application/json", "Referer": "https://leetcode.com"},
            timeout=10
        )
        if res.ok:
            data = res.json().get("data", {}).get("matchedUser", {})
            if data:
                stats = data.get("submitStats", {}).get("acSubmissionNum", [])
                solved = {s["difficulty"]: s["count"] for s in stats}
                return {
                    "type": "leetcode",
                    "username": username,
                    "ranking": data.get("profile", {}).get("ranking", "N/A"),
                    "easy_solved": solved.get("Easy", 0),
                    "medium_solved": solved.get("Medium", 0),
                    "hard_solved": solved.get("Hard", 0),
                    "total_solved": solved.get("All", 0),
                }
    except Exception as e:
        pass

    return {"type": "leetcode", "username": username, "error": "Could not fetch stats"}

# ── Portfolio / general website scraper ────────────────────────────────────

def scrape_website(url: str) -> dict:
    """Scrape general portfolio or website for relevant content."""
    try:
        res = requests.get(url, headers=HEADERS, timeout=8, allow_redirects=True)
        if not res.ok:
            return {"type": "website", "url": url, "error": f"HTTP {res.status_code}"}

        soup = BeautifulSoup(res.text, "lxml")

        # Remove noise
        for tag in soup(["script", "style", "nav", "footer", "header", "aside"]):
            tag.decompose()

        title = soup.find("title")
        title_text = title.get_text(strip=True) if title else ""

        # Get meta description
        meta = soup.find("meta", attrs={"name": "description"})
        meta_desc = meta.get("content", "") if meta else ""

        # Get main text content — first 800 chars
        body = soup.get_text(separator=" ", strip=True)
        body = re.sub(r'\s{2,}', ' ', body)[:800]

        # Extract headings for structure
        headings = [h.get_text(strip=True) for h in soup.find_all(["h1","h2","h3"])[:6]]

        return {
            "type": "website",
            "url": url,
            "title": title_text,
            "description": meta_desc,
            "headings": headings,
            "content_snippet": body,
        }
    except Exception as e:
        return {"type": "website", "url": url, "error": str(e)}

# ── LinkedIn (limited — just extract name from URL) ────────────────────────

def scrape_linkedin(url: str) -> dict:
    """
    LinkedIn blocks scrapers. We extract username from URL only.
    The AI will ask about LinkedIn experience based on resume text instead.
    """
    parsed = urlparse(url)
    parts = parsed.path.strip("/").split("/")
    username = ""
    if "in" in parts:
        idx = parts.index("in")
        if idx + 1 < len(parts):
            username = parts[idx + 1]

    return {
        "type": "linkedin",
        "url": url,
        "username": username,
        "note": "LinkedIn blocks automated scraping. Using resume text for LinkedIn experience questions."
    }

# ── Main orchestrator ───────────────────────────────────────────────────────

def scrape_resume_links(resume_text: str) -> dict:
    """
    Main function — called after resume upload.
    Extracts and scrapes all links found in resume.
    Returns structured data for interview agent.
    """
    urls = extract_urls(resume_text)
    if not urls:
        return {"urls_found": 0, "scraped": [], "summary": "No links found in resume."}

    results = []
    for url in urls[:8]:  # Max 8 URLs to avoid timeout
        url_type = classify_url(url)
        try:
            if url_type == "github":
                data = scrape_github(url)
            elif url_type == "leetcode":
                data = scrape_leetcode(url)
            elif url_type == "linkedin":
                data = scrape_linkedin(url)
            else:
                data = scrape_website(url)
            results.append({"url": url, **data})
        except Exception as e:
            results.append({"url": url, "type": url_type, "error": str(e)})
        time.sleep(0.3)  # Be polite to servers

    summary = _build_summary(results)
    return {
        "urls_found": len(urls),
        "urls_scraped": len(results),
        "scraped": results,
        "summary": summary
    }

def _build_summary(results: list) -> str:
    """Build a text summary for the interview agent to use."""
    parts = []
    for r in results:
        t = r.get("type", "")
        if "error" in r and t not in ["linkedin"]:
            continue

        if t == "github":
            repos = r.get("repos", [])
            repo_names = [f"{rp['name']} ({rp.get('language','?')})" for rp in repos[:4]]
            parts.append(
                f"GitHub @{r.get('username')}: {r.get('public_repos', 0)} public repos. "
                f"Notable projects: {', '.join(repo_names)}. "
                f"Bio: {r.get('bio', 'N/A')}."
            )
            # Add README snippets
            for rp in repos[:2]:
                if rp.get("readme_snippet"):
                    parts.append(f"Project '{rp['name']}' README: {rp['readme_snippet'][:200]}...")

        elif t == "github_repo":
            parts.append(
                f"GitHub repo '{r.get('name')}': {r.get('description','')}. "
                f"Language: {r.get('language','?')}. "
                f"README: {r.get('readme_snippet','')[:200]}"
            )

        elif t == "leetcode":
            parts.append(
                f"LeetCode @{r.get('username')}: "
                f"Solved {r.get('total_solved', 0)} problems "
                f"(Easy: {r.get('easy_solved',0)}, Medium: {r.get('medium_solved',0)}, Hard: {r.get('hard_solved',0)}). "
                f"Ranking: {r.get('ranking','N/A')}."
            )

        elif t == "linkedin":
            parts.append(f"LinkedIn profile found: {r.get('url')}. Will ask about experience from resume.")

        elif t == "website":
            parts.append(
                f"Portfolio site '{r.get('title',r.get('url',''))}': "
                f"{r.get('description','')} "
                f"Sections: {', '.join(r.get('headings',[])[:3])}."
            )

    return "\n".join(parts) if parts else "Links found but could not extract meaningful content."

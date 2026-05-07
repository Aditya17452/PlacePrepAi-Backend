# Resume Link Scraper — Integration Guide

## Install dependencies (run once)
```
pip install requests beautifulsoup4 lxml
```

## Step 1 — Create the table (run once)
```
python create_profile_table.py
```

## Step 2 — Copy scraper file
Copy `resume_scraper.py` → `C:\Users\DELL\placeprep-backend\services\resume_scraper.py`

## Step 3 — Update resume.py
Open `backend/api/resume.py`

Find the section after resume text is extracted (after pdfplumber). 
Add this code block right after `resume_text` is ready:

```python
from services.resume_scraper import scrape_resume_links

# Scrape links from resume
print("🔍 Scraping resume links...")
try:
    scrape_data = scrape_resume_links(resume_text)
    links_summary = scrape_data.get("summary", "")
    urls_found = scrape_data.get("urls_found", 0)
    print(f"✅ Found {urls_found} URLs, scraped {scrape_data.get('urls_scraped',0)}")
    if student_id and links_summary:
        db.execute(
            text("""INSERT OR REPLACE INTO student_profile_data
                    (student_id, scraped_summary, urls_found, updated_at)
                    VALUES (:sid, :summary, :urls, datetime('now'))"""),
            {"sid": student_id, "summary": links_summary, "urls": urls_found}
        )
        db.commit()
except Exception as e:
    print(f"⚠️ Link scraping failed (non-fatal): {e}")
    links_summary = ""
```

## Step 4 — Update interview_agent.py
Open `backend/agents/interview_agent.py`

Add this function above the WebSocket handler:
```python
def get_student_profile(db, student_id: str) -> str:
    try:
        from sqlalchemy import text
        row = db.execute(
            text("SELECT scraped_summary FROM student_profile_data WHERE student_id = :sid"),
            {"sid": student_id}
        ).fetchone()
        return row[0] if row and row[0] else ""
    except:
        return ""
```

Then find where you build the Groq messages list and add before it:
```python
profile_context = get_student_profile(db, student_id)
if profile_context:
    enhanced_system = SYSTEM_PROMPT + f"""

CANDIDATE PROFILE (from their GitHub/portfolio):
{profile_context}

Ask questions referencing their ACTUAL projects. Example:
"I see you built [project name] using [tech] — walk me through the architecture."
"Your LeetCode shows [X] medium problems solved — how do you approach dynamic programming?"
"""
else:
    enhanced_system = SYSTEM_PROMPT
```

Replace `SYSTEM_PROMPT` with `enhanced_system` in your messages list.

## What gets scraped

| Source | Data extracted |
|---|---|
| GitHub profile | Top 6 repos, languages, README snippets, bio |
| GitHub repo URL | Specific repo description + README |
| LeetCode | Easy/Medium/Hard solved count, ranking |
| Portfolio site | Title, headings, description, text snippet |
| LinkedIn | URL only (blocks bots) — resume text used instead |

## How it improves interviews

Before scraping:
> "Tell me about your technical projects."

After scraping:
> "I can see your GitHub has PlacePrepAI built with FastAPI and React, and another project using LangChain. Can you explain the WebSocket architecture in PlacePrepAI and why you chose FastAPI over Django?"

This is the USP — personalized questions from real project data.

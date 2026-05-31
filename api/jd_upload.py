"""
api/jd_upload.py  ── Student custom JD upload & resume-match feature
New endpoints (all backward-compatible, touch nothing else):
  POST   /api/jd/submit
  GET    /api/jd/list/{student_id}
  GET    /api/jd/status/{jd_id}
  DELETE /api/jd/delete/{jd_id}?student_id=...
"""

import json
import uuid
import asyncio

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session
from sqlalchemy import text
from pydantic import BaseModel

from models.database import get_db, SessionLocal, StudentJD, Base, engine
from core.config import settings

router = APIRouter(prefix="/api/jd", tags=["student_jds"])


# ── DB helper ─────────────────────────────────────────────────────────────────

def _ensure_student_jds_table(db: Session) -> None:
    """Idempotent: ensure student_jds table exists via SQLAlchemy (works on any DB)."""
    try:
        StudentJD.__table__.create(bind=engine, checkfirst=True)
    except Exception:
        pass  # already exists — safe to ignore



# ── Pydantic schemas ───────────────────────────────────────────────────────────

class SubmitJDRequest(BaseModel):
    student_id:   str
    company_name: str
    role_title:   str
    jd_text:      str


# ── Endpoints ─────────────────────────────────────────────────────────────────

@router.post("/submit")
async def submit_jd(req: SubmitJDRequest, db: Session = Depends(get_db)):
    """
    Student submits a custom JD.
    Returns immediately with {id, status: 'pending'}.
    Background task does the Groq resume-match and updates the row.
    """
    _ensure_student_jds_table(db)

    jd_text_clean = req.jd_text.strip()
    if len(jd_text_clean) < 50:
        raise HTTPException(status_code=400, detail="JD text too short (min 50 chars)")

    jd_id = str(uuid.uuid4())

    db.execute(text("""
        INSERT INTO student_jds
            (id, student_id, company_name, role_title, jd_text, status)
        VALUES
            (:id, :sid, :company, :role, :jd_text, 'pending')
    """), {
        "id":      jd_id,
        "sid":     req.student_id,
        "company": req.company_name.strip(),
        "role":    req.role_title.strip(),
        "jd_text": jd_text_clean,
    })
    db.commit()

    # Fire-and-forget background matching
    asyncio.create_task(
        _match_jd_async(jd_id, req.student_id, jd_text_clean)
    )

    return {"id": jd_id, "status": "pending"}


@router.get("/list/{student_id}")
def get_student_jds(student_id: str, db: Session = Depends(get_db)):
    """Return all JDs submitted by a student, newest first."""
    _ensure_student_jds_table(db)

    rows = db.execute(text("""
        SELECT id, company_name, role_title,
               match_pct, matched_skills, missing_skills,
               advice, status, created_at
        FROM   student_jds
        WHERE  student_id = :sid
        ORDER  BY created_at DESC
    """), {"sid": student_id}).fetchall()

    return [
        {
            "id":             r[0],
            "company_name":   r[1],
            "role_title":     r[2],
            "match_pct":      r[3] or 0,
            "matched_skills": _safe_json(r[4]),
            "missing_skills": _safe_json(r[5]),
            "advice":         r[6] or "",
            "status":         r[7],
            "created_at":     r[8],
        }
        for r in rows
    ]


@router.get("/status/{jd_id}")
def get_jd_status(jd_id: str, db: Session = Depends(get_db)):
    """Poll for the current match status of a single JD."""
    _ensure_student_jds_table(db)

    row = db.execute(text("""
        SELECT id, company_name, role_title,
               match_pct, matched_skills, missing_skills,
               advice, status
        FROM   student_jds
        WHERE  id = :id
    """), {"id": jd_id}).fetchone()

    if not row:
        raise HTTPException(status_code=404, detail="JD not found")

    return {
        "id":             row[0],
        "company_name":   row[1],
        "role_title":     row[2],
        "match_pct":      row[3] or 0,
        "matched_skills": _safe_json(row[4]),
        "missing_skills": _safe_json(row[5]),
        "advice":         row[6] or "",
        "status":         row[7],
    }


@router.delete("/delete/{jd_id}")
def delete_student_jd(
    jd_id:      str,
    student_id: str,
    db:         Session = Depends(get_db),
):
    """Delete a JD (only if it belongs to the given student)."""
    _ensure_student_jds_table(db)

    db.execute(text("""
        DELETE FROM student_jds
        WHERE  id = :id AND student_id = :sid
    """), {"id": jd_id, "sid": student_id})
    db.commit()

    return {"success": True}


# ── Background matching task ───────────────────────────────────────────────────

async def _match_jd_async(jd_id: str, student_id: str, jd_text: str) -> None:
    """
    Fetches the student's stored resume_text, calls Groq to get a match result,
    then updates the student_jds row.  Runs as a background asyncio task.
    """
    db = SessionLocal()
    try:
        # Get resume text from students table
        row = db.execute(
            text("SELECT resume_text FROM students WHERE id = :sid"),
            {"sid": student_id}
        ).fetchone()
        resume_text = (row[0] or "").strip() if row else ""

        if not resume_text:
            db.execute(text("""
                UPDATE student_jds
                SET    status  = 'complete',
                       match_pct = 0,
                       advice  = 'No resume found. Please upload your resume first.'
                WHERE  id = :id
            """), {"id": jd_id})
            db.commit()
            print(f"⚠️  JD {jd_id[:8]}: no resume for student {student_id[:8]}")
            return

        result = await _groq_match(resume_text, jd_text)

        db.execute(text("""
            UPDATE student_jds
            SET    match_pct      = :pct,
                   matched_skills = :matched,
                   missing_skills = :missing,
                   advice         = :advice,
                   status         = 'complete'
            WHERE  id = :id
        """), {
            "pct":     int(result.get("match_pct", 0)),
            "matched": json.dumps(result.get("matching_skills", [])),
            "missing": json.dumps(result.get("missing_skills", [])),
            "advice":  result.get("advice", ""),
            "id":      jd_id,
        })
        db.commit()
        print(f"✅ JD {jd_id[:8]} matched: {result.get('match_pct')}%")

    except Exception as e:
        print(f"❌ JD match async error for {jd_id[:8]}: {e}")
        try:
            db.execute(
                text("UPDATE student_jds SET status='failed' WHERE id=:id"),
                {"id": jd_id}
            )
            db.commit()
        except Exception:
            pass
    finally:
        db.close()


async def _groq_match(resume_text: str, jd_text: str) -> dict:
    """Call Groq LLM to match a resume against a JD.  Returns a dict."""
    from groq import AsyncGroq

    client = AsyncGroq(api_key=settings.GROQ_API_KEY)

    system_prompt = """\
You are a strict resume-to-job-description matcher.
Analyse the resume carefully against the job description based on ACTUAL skills present.

RULES:
- Only give a high match % if the resume ACTUALLY has the required skills.
- A non-tech or irrelevant resume should score 10-30%.
- Be honest — do NOT give everyone 70%+.
- Return ONLY valid JSON, no markdown, no backticks.

Required format:
{
  "match_pct": 75,
  "matching_skills": ["Python", "SQL"],
  "missing_skills": ["Docker", "Kubernetes"],
  "advice": "One specific, actionable sentence about this match."
}"""

    user_msg = (
        f"Resume:\n{resume_text[:1500]}\n\n"
        f"Job Description:\n{jd_text[:1500]}\n\n"
        "Analyse and return the JSON result."
    )

    for attempt in range(3):
        try:
            resp = await asyncio.wait_for(
                client.chat.completions.create(
                    model=settings.GROQ_MODEL,
                    messages=[
                        {"role": "system", "content": system_prompt},
                        {"role": "user",   "content": user_msg},
                    ],
                    max_tokens=400,
                    temperature=0.2,
                ),
                timeout=22.0,
            )
            raw = (resp.choices[0].message.content or "").strip()
            raw = raw.replace("```json", "").replace("```", "").strip()
            parsed = json.loads(raw)
            # Clamp match_pct to 0-100
            parsed["match_pct"] = max(0, min(100, int(parsed.get("match_pct", 0))))
            return parsed

        except asyncio.TimeoutError:
            print(f"⏱️  Groq JD match timeout (attempt {attempt + 1})")
        except json.JSONDecodeError as e:
            print(f"⚠️  Groq JD match JSON parse failed (attempt {attempt + 1}): {e}")
        except Exception as e:
            print(f"⚠️  Groq JD match error (attempt {attempt + 1}): {e}")

        if attempt < 2:
            await asyncio.sleep(2)

    # Keyword fallback so the card doesn't stay stuck at 0%
    return {
        "match_pct": 0,
        "matching_skills": [],
        "missing_skills": [],
        "advice": "Could not analyse match automatically. Try uploading your resume and retrying.",
    }


# ── Utility ───────────────────────────────────────────────────────────────────

def _safe_json(value: str | None) -> list:
    try:
        return json.loads(value) if value else []
    except Exception:
        return []

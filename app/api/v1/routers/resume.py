"""Resume upload, extraction, link enrichment and JD matching.

Fixes in Phase 1:

* **Authenticated.** `student_id` came from a query parameter, so anyone could overwrite
  anyone's resume text and read their JD matches.
* **Size and type limits.** There were none; the handler read the whole upload into
  memory and, if nothing parsed, decoded the bytes as UTF-8 and stored the garbage.
* **Real extraction.** `pdfplumber` was called but never installed, so production used
  PyPDF2 and truncated to 3,000 characters.
* **Non-blocking enrichment.** Link fetching used `requests` plus `time.sleep` inside an
  async handler; it is now an SSRF-guarded async client.
* **No request-time DDL.** It used to `CREATE TABLE IF NOT EXISTS student_profile_data`
  on every upload.
"""

from __future__ import annotations

import asyncio
from time import perf_counter

from fastapi import APIRouter, File, HTTPException, Request, UploadFile, status
from pydantic import BaseModel, Field

from app.api.deps import CurrentUser, DbSession
from app.core import audit, ratelimit
from app.core.config import settings
from app.core.llm import LLMError, Task, complete_structured
from app.core.logging_setup import get_logger
from app.models import JobDescription, Student, StudentProfileData, utcnow
from app.schemas import JDListItem, JDMatchOut, ResumeUploadResponse
from app.services import candidate_links, document_text, storage

log = get_logger("resume")

router = APIRouter(prefix="/api/resume", tags=["resume"])

#: How much resume text goes into a matching prompt. The full text is stored; prompts are
#: bounded for cost, not because the extractor cannot read more.
_PROMPT_CHARS = 6_000


class _Match(BaseModel):
    jd_id: str
    match_pct: int = Field(ge=0, le=100)
    matching_skills: list[str] = Field(default_factory=list, max_length=12)
    missing_skills: list[str] = Field(default_factory=list, max_length=12)
    advice: str = Field(default="", max_length=400)


class _MatchSet(BaseModel):
    ranked: list[_Match] = Field(min_length=1, max_length=20)


@router.post("/upload", response_model=ResumeUploadResponse)
async def upload_resume(
    request: Request,
    principal: CurrentUser,
    db: DbSession,
    resume: UploadFile = File(...),
) -> ResumeUploadResponse:
    """Store a resume, enrich it from the candidate's public profiles, match it to roles."""
    await ratelimit.limit_upload(request, principal.id)

    t_start = perf_counter()
    data = await _read_capped(resume)
    try:
        text = document_text.extract(data, resume.filename or "")
    except document_text.UnsupportedDocument as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from exc

    if len(text) < 100:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            "That file has very little readable text. Please upload your full resume.",
        )

    student = db.query(Student).filter(Student.id == principal.id).first()
    if student is None:  # pragma: no cover
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Account not found")

    jds = db.query(JobDescription).filter(JobDescription.is_active.is_(True)).all()

    # These three are independent network calls — a Supabase upload, profile scraping,
    # and the matching model. Run concurrently instead of one after another: they used to
    # be sequential, which is most of why this endpoint took 9-10 seconds. The database
    # session is deliberately not touched inside them; a Session is not safe to share
    # across concurrent tasks, so every write happens after they finish.
    async def _store() -> None:
        if not storage.is_configured():
            return
        try:
            await storage.upload(
                bucket=settings.SUPABASE_RESUME_BUCKET,
                path=f"{principal.id}/resume-{int(utcnow().timestamp())}.pdf",
                data=data,
                content_type=resume.content_type or "application/pdf",
            )
        except storage.StorageUnavailable as exc:
            # Losing the file copy is acceptable; losing the upload is not.
            log.warning("resume_file_not_stored", error=str(exc)[:160])

    t_parse = perf_counter()
    _, scrape, suggestions = await asyncio.gather(
        _store(),
        candidate_links.scrape_resume_links(text),
        _rank_jds(text, jds, student_id=principal.id),
    )
    t_io = perf_counter()

    student.resume_text = text

    if scrape.summary:
        profile = (
            db.query(StudentProfileData)
            .filter(StudentProfileData.student_id == principal.id)
            .first()
        )
        if profile is None:
            profile = StudentProfileData(student_id=principal.id)
            db.add(profile)
        profile.scraped_summary = scrape.summary
        profile.urls_found = scrape.urls_found
        profile.updated_at = utcnow()

    audit.record(
        db,
        action="resume.uploaded",
        actor_id=principal.id,
        actor_role=principal.role.value,
        detail={
            "chars": len(text),
            "urls_found": scrape.urls_found,
            "urls_fetched": scrape.urls_fetched,
        },
        ip=ratelimit.client_ip(request),
    )
    db.commit()

    log.info(
        "resume_upload_timing",
        bytes=len(data),
        chars=len(text),
        parse_ms=int((t_parse - t_start) * 1000),
        io_ms=int((t_io - t_parse) * 1000),
        total_ms=int((perf_counter() - t_start) * 1000),
        jds=len(jds),
        urls_fetched=scrape.urls_fetched,
    )
    return ResumeUploadResponse(status="ready", suggestions=suggestions)


@router.get("/jds", response_model=list[JDListItem])
async def list_jds(db: DbSession) -> list[JDListItem]:
    rows = (
        db.query(JobDescription)
        .filter(JobDescription.is_active.is_(True))
        .order_by(JobDescription.role_title)
        .all()
    )
    return [
        JDListItem(id=str(jd.id), company=jd.company_name, role=jd.role_title)
        for jd in rows
    ]


# ── Helpers ───────────────────────────────────────────────────────────────────


async def _read_capped(upload: UploadFile) -> bytes:
    """Read the upload, refusing anything over the configured limit.

    Read in chunks so an oversized file is rejected without first buffering all of it.
    """
    limit = settings.MAX_UPLOAD_BYTES
    chunks: list[bytes] = []
    total = 0
    while True:
        chunk = await upload.read(64 * 1024)
        if not chunk:
            break
        total += len(chunk)
        if total > limit:
            raise HTTPException(
                status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
                f"That file is larger than {limit // (1024 * 1024)} MB.",
            )
        chunks.append(chunk)

    if total == 0:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "That file is empty.")
    return b"".join(chunks)


async def _rank_jds(
    resume_text: str, jds: list[JobDescription], *, student_id: str
) -> list[JDMatchOut]:
    """Rank a resume against already-loaded JDs.

    Takes the rows rather than a Session so it can run concurrently with the other
    network calls in the upload handler — a Session must not be used from two tasks at
    once.
    """
    if not jds:
        return []

    catalogue = "\n\n".join(
        f"JD_ID: {jd.id}\nRole: {jd.role_title}\nRequirements: {(jd.jd_text or '')[:400]}"
        for jd in jds
    )
    system = """You match a resume against job descriptions on the skills actually present.

Be honest: only give a high percentage when the resume genuinely has the required skills.
A non-technical or irrelevant resume scores 10-30. Do not give everyone 70%+.

Reply as JSON: {"ranked": [{"jd_id": "<exact id>", "match_pct": 0-100,
"matching_skills": [...], "missing_skills": [...], "advice": "one sentence"}]}
Include every JD_ID listed, using the exact ids given."""

    user = (
        f"RESUME:\n{resume_text[:_PROMPT_CHARS]}\n\n"
        f"JOB DESCRIPTIONS ({len(jds)}):\n{catalogue}\n\n"
        f"Rank all {len(jds)}."
    )

    try:
        matches = await complete_structured(
            task=Task.EXTRACTOR,
            system=system,
            user=user,
            schema=_MatchSet,
            temperature=0.2,
            max_tokens=1_200,
            student_id=student_id,
        )
        ranked = matches.ranked
    except LLMError as exc:
        log.warning("resume_match_failed", error=str(exc)[:200])
        ranked = _keyword_fallback(resume_text, jds)

    by_id = {str(jd.id): jd for jd in jds}
    out: list[JDMatchOut] = []
    for match in ranked:
        jd = by_id.get(match.jd_id)
        if jd is None:
            continue
        out.append(
            JDMatchOut(
                company=jd.company_name,
                role=jd.role_title,
                matchScore=match.match_pct,
                status=_band(match.match_pct),
                matchedSkills=match.matching_skills,
                missingSkills=match.missing_skills,
                reasoning=match.advice,
                jdId=str(jd.id),
            )
        )
    out.sort(key=lambda m: m.matchScore, reverse=True)
    return out


def _band(pct: int) -> str:
    if pct >= 80:
        return "Best Match"
    if pct >= 60:
        return "Good Match"
    if pct >= 40:
        return "Possible Match"
    return "Low Match"


_KEYWORDS = (
    "python", "java", "javascript", "typescript", "react", "node", "sql", "mysql",
    "postgresql", "mongodb", "redis", "machine learning", "deep learning", "tensorflow",
    "pytorch", "pandas", "numpy", "docker", "kubernetes", "aws", "azure", "gcp", "git",
    "ci/cd", "html", "css", "rest", "nlp", "computer vision", "opencv", "llm", "rag",
    "spark", "hadoop", "etl", "airflow", "kafka", "c++", "go", "rust", "kotlin", "swift",
    "linux", "tableau", "power bi", "excel",
)


def _keyword_fallback(resume_text: str, jds: list[JobDescription]) -> list[_Match]:
    """Overlap of known skill keywords, used when the model is unavailable.

    Labelled as such in the advice text so a student is not told a keyword count is an
    AI assessment.
    """
    resume_lower = resume_text.lower()
    results: list[_Match] = []
    for jd in jds:
        jd_lower = f"{jd.role_title} {jd.jd_text or ''}".lower()
        required = [k for k in _KEYWORDS if k in jd_lower]
        if not required:
            results.append(
                _Match(jd_id=str(jd.id), match_pct=30, advice="Keyword comparison only.")
            )
            continue
        matched = [k for k in required if k in resume_lower]
        missing = [k for k in required if k not in resume_lower]
        pct = min(int(len(matched) / len(required) * 100), 90)
        results.append(
            _Match(
                jd_id=str(jd.id),
                match_pct=pct,
                matching_skills=[k.title() for k in matched[:6]],
                missing_skills=[k.title() for k in missing[:6]],
                advice="Keyword comparison only — the AI matcher was unavailable. "
                "Upload again for a full assessment.",
            )
        )
    results.sort(key=lambda m: m.match_pct, reverse=True)
    return results

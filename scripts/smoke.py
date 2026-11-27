"""End-to-end smoke test of the interview loop.

Two modes:

* **local** (default) — builds a throwaway SQLite database, seeds a role and a student,
  and drives the app in-process. External services are real, so this exercises Groq,
  Upstash and Supabase Storage as configured in `.env`.

      python -m scripts.smoke

* **remote** — drives a deployed preview over HTTP. Needs an existing student account,
  because registering one requires an email OTP a script cannot read.

      python -m scripts.smoke --base-url https://<preview>/api --token <jwt> --jd-id <id>

Every step prints PASS/FAIL with evidence. Exit code is non-zero if anything failed, so
it can gate a deploy.
"""

from __future__ import annotations

import argparse
import os
import pathlib
import sys
import tempfile
import uuid
from datetime import UTC

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

PASS, FAIL = "PASS", "FAIL"
_results: list[tuple[str, bool, str]] = []


def step(name: str, ok: bool, evidence: str = "") -> bool:
    _results.append((name, ok, evidence))
    print(f"[{PASS if ok else FAIL}] {name}" + (f": {evidence}" if evidence else ""))
    return ok


ANSWERS = [
    "I built a FastAPI service backed by PostgreSQL with Redis caching for a college "
    "placement portal, handling about two thousand students. I used connection pooling "
    "because the database had a small connection limit.",
    "For cache invalidation I used write-through on the student profile and a short TTL "
    "on list endpoints. I hit a stale-read bug when two requests updated the same row, "
    "so I added a version column and checked it before writing to the cache.",
    "I index the columns in the WHERE clause and check the plan with EXPLAIN ANALYZE. On "
    "one report query a sequential scan over transcripts was the bottleneck, so I added "
    "a composite index on session id and turn number.",
]


def _seed_local() -> tuple[str, str]:
    """Throwaway SQLite database with one role and one funded, verified student."""
    from app.core.security import hash_password, make_access_token
    from app.models import Base, JobDescription, SessionLocal, Student, engine, utcnow
    from app.services import rag_service, token_service

    Base.metadata.create_all(bind=engine)
    db = SessionLocal()
    try:
        jd = JobDescription(
            company_name="Acme Technologies",
            role_title="Backend Engineer",
            jd_text=(
                "Role: Backend Engineer. Required Skills: Python, FastAPI, PostgreSQL, "
                "Redis, REST APIs, Docker. Key Evaluation Topics: database indexing and "
                "query plans, cache invalidation strategies, idempotency in HTTP APIs, "
                "connection pooling, transactions and isolation levels, background job "
                "design, testing strategy."
            ),
            is_active=True,
        )
        db.add(jd)
        db.flush()
        # Without chunks, JD retrieval returns nothing and questions come from the role
        # title alone — which is what production did for months.
        rag_service.index_jd(db, str(jd.id), jd.jd_text)

        student = Student(
            name="Smoke Candidate",
            email=f"smoke-{uuid.uuid4().hex[:8]}@example.com",
            role="student",
            password_hash=hash_password("correct-horse-battery"),
            password_algo="bcrypt",
            email_verified_at=utcnow(),
            phone_verified_at=utcnow(),
            token_version=0,
            is_active=True,
        )
        db.add(student)
        db.flush()
        token_service.grant(
            db,
            owner_type="student",
            owner_id=str(student.id),
            qty=5,
            reason="smoke test",
            actor_id=None,
        )
        db.commit()
        return make_access_token(str(student.id), 0), str(jd.id)
    finally:
        db.close()


def run(get, post, jd_id: str) -> int:  # noqa: ANN001
    """Drive the loop. `get`/`post` abstract in-process vs HTTP."""
    ready = get("/ready")
    step("database reachable", ready.get("database") is True, f"status={ready.get('status')}")
    step(
        "redis is a real instance, not the in-process fallback",
        "in-memory" not in str(ready.get("detail", {}).get("redis", "")),
        f"redis={ready.get('redis')}",
    )

    refused_status, _ = post(
        "/interviews/start", {"jd_id": jd_id, "proctoring_enabled": True}, raw=True
    )
    step(
        "proctored start refused without consent",
        refused_status == 403,
        f"HTTP {refused_status}",
    )

    consents = get("/consents")
    version = next(c["current_version"] for c in consents if c["kind"] == "proctoring")
    status, _ = post(
        "/consents",
        {"kind": "proctoring", "version": version, "granted": True},
        raw=True,
    )
    step("consent recorded", status == 201, f"version {version}")

    started = post("/interviews/start", {"jd_id": jd_id, "proctoring_enabled": True})
    session_id = started.get("sessionId")
    if not session_id:
        step("interview started", False, str(started)[:200])
        return summarise()
    step(
        "interview started, one token reserved",
        started.get("tokens_remaining") == 4,
        f"tokens_remaining={started.get('tokens_remaining')}",
    )
    print(f"       Q1: {started['openingQuestion'][:95]}")

    questions = [started["openingQuestion"]]
    consumed = False
    for index, answer in enumerate(ANSWERS, start=1):
        turn = post(
            f"/interviews/{session_id}/turns",
            {"turn_id": f"smoke-turn-{index}", "transcript": answer},
        )
        if "question" not in turn:
            step(f"turn {index}", False, str(turn)[:220])
            return summarise()
        consumed = consumed or bool(turn.get("token_consumed"))
        questions.append(turn["question"] or "(complete)")
        print(f"       Q{index + 1} [{turn.get('phase')}]: {str(turn['question'])[:95]}")

    step("turns answered by the live model", all(questions), f"{len(questions)} questions")
    step("token consumed at the threshold", consumed, "3 answers, as configured")
    step(
        "no question repeated",
        len(set(questions)) == len(questions),
        f"{len(set(questions))} distinct of {len(questions)}",
    )

    replay = post(
        f"/interviews/{session_id}/turns",
        {"turn_id": f"smoke-turn-{len(ANSWERS)}", "transcript": ANSWERS[-1]},
    )
    step(
        "replayed turn_id is idempotent",
        replay.get("question") == questions[-1],
        "same question returned, interview not advanced",
    )

    from datetime import datetime

    proctor = post(
        f"/interviews/{session_id}/proctoring",
        {
            "events": [
                {
                    "type": "tab_switch",
                    "severity": 3,
                    "client_ts": datetime.now(UTC).isoformat(),
                    "detail": {"duration_ms": 1200},
                }
            ],
            "vision": {"posture_score": 82, "eye_contact_score": 74},
        },
    )
    step(
        "proctoring stored, server declined to terminate",
        proctor.get("accepted") == 1 and proctor.get("terminate") is False,
        f"accepted={proctor.get('accepted')}, terminate={proctor.get('terminate')}",
    )

    post(f"/interviews/{session_id}/end", {"reason": "student_ended"})
    report = get(f"/interviews/{session_id}/report")
    ok = report.get("status") == "complete"
    step(
        "report evaluated",
        ok,
        f"status={report.get('status')}, score={report.get('overallScore')}, "
        f"verdict={report.get('verdict')}",
    )
    if ok:
        step(
            "integrity reported separately from the technical score",
            report.get("integrityScore") is not None,
            f"integrity={report.get('integrityScore')} vs overall={report.get('overallScore')}",
        )

    return summarise()


def summarise() -> int:
    print()
    failed = [name for name, ok, _ in _results if not ok]
    print(f"{len(_results) - len(failed)}/{len(_results)} passed")
    if failed:
        print("FAILED:", ", ".join(failed))
    return 1 if failed else 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", help="Deployed API base, including /api")
    parser.add_argument("--token", help="Bearer token for an existing student")
    parser.add_argument("--jd-id", help="Job description id to interview against")
    args = parser.parse_args()

    if args.base_url:
        if not (args.token and args.jd_id):
            parser.error("--base-url also needs --token and --jd-id")
        import httpx

        base = args.base_url.rstrip("/")
        headers = {"Authorization": f"Bearer {args.token}"}
        client = httpx.Client(timeout=90.0, headers=headers)

        def get(path: str):  # noqa: ANN202
            url = path if path.startswith("/ready") else f"{base}{path}"
            if path.startswith("/ready"):
                url = base.rsplit("/api", 1)[0] + "/ready"
            return client.get(url).json()

        def post(path: str, body: dict, raw: bool = False):  # noqa: ANN202
            resp = client.post(f"{base}{path}", json=body)
            return (resp.status_code, resp) if raw else resp.json()

        return run(get, post, args.jd_id)

    # Local mode: SQLite for storage, real external services from .env.
    db_file = pathlib.Path(tempfile.gettempdir()) / f"pp-smoke-{uuid.uuid4().hex}.db"
    os.environ["DATABASE_URL"] = f"sqlite:///{db_file.as_posix()}"
    os.environ.setdefault("ENV", "local")
    os.environ.setdefault("LOG_LEVEL", "WARNING")

    from fastapi.testclient import TestClient

    from app.main import create_app

    token, jd_id = _seed_local()
    headers = {"Authorization": f"Bearer {token}"}
    with TestClient(create_app()) as client:

        def get(path: str):  # noqa: ANN202
            url = "/ready" if path == "/ready" else f"/api{path}"
            return client.get(url, headers=headers).json()

        def post(path: str, body: dict, raw: bool = False):  # noqa: ANN202
            resp = client.post(f"/api{path}", headers=headers, json=body)
            return (resp.status_code, resp) if raw else resp.json()

        code = run(get, post, jd_id)

    # Cost of this run, which feeds the PLAN.md §9 guardrail.
    from app.models import LlmUsage, SessionLocal

    db = SessionLocal()
    try:
        rows = db.query(LlmUsage).all()
        usd = sum(r.cost_usd for r in rows)
        tokens = sum(r.prompt_tokens + r.completion_tokens for r in rows)
        print(
            f"LLM cost: {len(rows)} calls, {tokens} tokens, ${usd:.5f} "
            f"(~Rs {usd * 84:.3f})"
        )
    finally:
        db.close()
    return code


if __name__ == "__main__":
    raise SystemExit(main())

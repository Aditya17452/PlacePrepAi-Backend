"""FastAPI application entry point.

What changed in Phase 1:

* Routers are imported normally. The previous five `try/except ImportError: print(...)`
  blocks meant a broken router disappeared from a running deployment with nothing but a
  log line — and one of them (`api.profile_links`) referred to a module that has never
  existed.
* No DDL at startup. `create_tables()` is gone; schema changes go through Alembic.
* CORS comes from configuration, with exact origins. It was `allow_origins=["*"]`
  alongside `allow_credentials=True`.
* `/health` is a liveness probe (no dependencies) and `/ready` actually checks the
  database and Redis, so a deploy that cannot reach Postgres fails its readiness gate
  instead of serving errors.
"""

from __future__ import annotations

from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from sqlalchemy import text

from api import (
    admin_tokens,
    auth,
    cron,
    interviews,
    jd_upload,
    officer,
    payments,
    resume,
    super_admin,
)
from core import kv
from core.config import settings
from core.logging_setup import RequestContextMiddleware, configure_logging, get_logger
from models.database import SessionLocal
from models.schemas import HealthResponse, ReadyResponse
from services.bootstrap import bootstrap_admins

VERSION = "2.0.0"

configure_logging()
log = get_logger("app")


@asynccontextmanager
async def lifespan(app: FastAPI):
    log.info(
        "startup",
        env=settings.ENV,
        version=VERSION,
        question_model=settings.LLM_MODEL_QUESTION,
    )

    if settings.SENTRY_DSN:
        import sentry_sdk

        sentry_sdk.init(
            dsn=settings.SENTRY_DSN,
            environment=settings.ENV,
            traces_sample_rate=0.1,
            # Scrub request bodies and headers; they carry resumes and bearer tokens.
            send_default_pii=False,
        )
        log.info("sentry_initialised")

    if not settings.is_production and kv.is_memory_backend():
        log.warning(
            "kv_in_memory",
            detail="No Redis configured. Session state is per-process — fine locally, "
            "broken behind more than one worker.",
        )

    # Promote the configured bootstrap emails to admin once, then the DB is authoritative.
    db = SessionLocal()
    try:
        promoted = bootstrap_admins(db)
        if promoted:
            log.info("admins_bootstrapped", emails=promoted)
    except Exception as exc:
        # A bootstrap failure must not stop the app from serving existing users.
        log.error("admin_bootstrap_failed", error=str(exc))
    finally:
        db.close()

    yield
    log.info("shutdown")


app = FastAPI(
    title="PlacePrep AI",
    version=VERSION,
    lifespan=lifespan,
    # The interactive docs expose every schema; keep them off the public production app.
    docs_url=None if settings.is_production else "/docs",
    redoc_url=None,
    openapi_url=None if settings.is_production else "/openapi.json",
)

app.add_middleware(RequestContextMiddleware)
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins,
    allow_origin_regex=settings.CORS_ORIGIN_REGEX or None,
    allow_credentials=True,
    allow_methods=["GET", "POST", "PATCH", "DELETE", "OPTIONS"],
    allow_headers=["Authorization", "Content-Type", "X-Request-ID"],
    expose_headers=["X-Request-ID"],
    max_age=600,
)

# ── Routers ───────────────────────────────────────────────────────────────────
app.include_router(auth.router)
app.include_router(resume.router)
app.include_router(jd_upload.router)
app.include_router(interviews.router)
app.include_router(admin_tokens.router)
app.include_router(payments.router)
app.include_router(officer.router)
app.include_router(super_admin.router)
app.include_router(cron.router)


# ── Error handling ────────────────────────────────────────────────────────────


@app.exception_handler(Exception)
async def unhandled_exception_handler(request: Request, exc: Exception) -> JSONResponse:
    """Log the detail, return a generic message.

    Several endpoints used to do `raise HTTPException(500, str(e))`, which leaked SQL
    fragments and connection strings to the browser.
    """
    log.exception("unhandled_error", path=request.url.path)
    return JSONResponse(
        status_code=500,
        content={"detail": "Something went wrong on our side. Please try again."},
    )


# ── Probes ────────────────────────────────────────────────────────────────────


@app.get("/health", response_model=HealthResponse, tags=["ops"])
async def health() -> HealthResponse:
    """Liveness. Deliberately checks nothing external."""
    return HealthResponse(status="ok", env=settings.ENV, version=VERSION)


@app.get("/ready", response_model=ReadyResponse, tags=["ops"])
async def ready() -> ReadyResponse:
    detail: dict[str, str] = {}

    db_ok = False
    db = SessionLocal()
    try:
        db.execute(text("SELECT 1"))
        db_ok = True
    except Exception as exc:
        detail["database"] = str(exc)[:200]
    finally:
        db.close()

    redis_ok = await kv.healthy()
    if not redis_ok:
        detail["redis"] = "ping failed"
    # Locally, the in-memory fallback is reported honestly rather than as a healthy Redis.
    if kv.is_memory_backend():
        detail["redis"] = "in-memory fallback (not for production)"

    return ReadyResponse(
        status="ready" if (db_ok and redis_ok) else "degraded",
        database=db_ok,
        redis=redis_ok,
        detail=detail,
    )


@app.get("/", tags=["ops"])
async def root() -> dict[str, str]:
    return {"service": "PlacePrep AI", "version": VERSION, "status": "running"}

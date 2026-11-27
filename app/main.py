"""Application factory and entry point.

`create_app()` builds the application, so tests and the Vercel entry point get the same
wiring rather than a module-level singleton assembled at import time.

Routers are imported normally. The previous version wrapped five of them in
`try/except ImportError: print(...)`, which meant a broken router silently disappeared
from a running deployment — and one of them referred to a module that never existed.
"""

from __future__ import annotations

from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from sqlalchemy import text

# Importing this registers every job handler with the queue. Handlers used to live inside
# the routers, so the queue silently had none if a router was not imported.
from app import workers  # noqa: F401
from app.api.v1.routers import (
    admin,
    auth,
    college_admin,
    cron,
    interviews,
    jd,
    payments,
    proctoring,
    resume,
    tokens,
)
from app.core import kv
from app.core.config import settings
from app.core.logging_setup import (
    RequestContextMiddleware,
    configure_logging,
    get_logger,
)
from app.models import SessionLocal
from app.schemas import HealthResponse, ReadyResponse
from app.services.bootstrap import bootstrap_admins

VERSION = "2.0.0"

configure_logging()
log = get_logger("app")

ROUTERS = (
    auth.router,
    resume.router,
    jd.router,
    interviews.router,
    proctoring.router,
    tokens.router,
    payments.router,
    college_admin.router,
    college_admin.allowance_router,
    admin.router,
    admin.tokens_router,
    cron.router,
)


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
            # Request bodies carry resumes and bearer tokens.
            send_default_pii=False,
        )
        log.info("sentry_initialised")

    if not settings.is_production and kv.is_memory_backend():
        log.warning(
            "kv_in_memory",
            detail="No Redis configured. Session state is per-process — fine locally, "
            "broken behind more than one worker.",
        )

    db = SessionLocal()
    try:
        promoted = bootstrap_admins(db)
        if promoted:
            log.info("admins_bootstrapped", emails=promoted)
    except Exception as exc:
        # A bootstrap failure must not stop the app serving existing users.
        log.error("admin_bootstrap_failed", error=str(exc))
    finally:
        db.close()

    yield
    log.info("shutdown")


def create_app() -> FastAPI:
    application = FastAPI(
        title="PlacePrep AI",
        version=VERSION,
        lifespan=lifespan,
        # The docs expose every schema; keep them off the public production app.
        docs_url=None if settings.is_production else "/docs",
        redoc_url=None,
        openapi_url=None if settings.is_production else "/openapi.json",
    )

    application.add_middleware(RequestContextMiddleware)
    application.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origins,
        allow_origin_regex=settings.CORS_ORIGIN_REGEX or None,
        allow_credentials=True,
        allow_methods=["GET", "POST", "PATCH", "DELETE", "OPTIONS"],
        allow_headers=["Authorization", "Content-Type", "X-Request-ID"],
        expose_headers=["X-Request-ID"],
        max_age=600,
    )

    for router in ROUTERS:
        application.include_router(router)

    _register_probes(application)
    _register_error_handler(application)
    return application


def _register_error_handler(application: FastAPI) -> None:
    @application.exception_handler(Exception)
    async def unhandled_exception_handler(
        request: Request, exc: Exception
    ) -> JSONResponse:
        """Log the detail, return a generic message.

        Several endpoints used to `raise HTTPException(500, str(e))`, which leaked SQL
        fragments and connection strings to the browser.
        """
        log.exception("unhandled_error", path=request.url.path)
        return JSONResponse(
            status_code=500,
            content={"detail": "Something went wrong on our side. Please try again."},
        )


def _register_probes(application: FastAPI) -> None:
    @application.get("/health", response_model=HealthResponse, tags=["ops"])
    async def health() -> HealthResponse:
        """Liveness. Deliberately checks nothing external."""
        return HealthResponse(status="ok", env=settings.ENV, version=VERSION)

    @application.get("/ready", response_model=ReadyResponse, tags=["ops"])
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
        # Report the local fallback honestly rather than as a healthy Redis.
        if kv.is_memory_backend():
            detail["redis"] = "in-memory fallback (not for production)"

        return ReadyResponse(
            status="ready" if (db_ok and redis_ok) else "degraded",
            database=db_ok,
            redis=redis_ok,
            detail=detail,
        )

    @application.get("/", tags=["ops"])
    async def root() -> dict[str, str]:
        return {"service": "PlacePrep AI", "version": VERSION, "status": "running"}


app = create_app()

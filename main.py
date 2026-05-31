import sys
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from importlib import import_module
from api.interviews import router as interview_router
from api.resume import router as resume_router
from api.auth import router as auth_router
from api.credits import router as credits_router
from models.database import create_tables
from core.config import settings

# ── Force UTF-8 stdout on Windows so emoji in print() doesn't crash ───────────
if sys.stdout.encoding and sys.stdout.encoding.lower() != "utf-8":
    import io
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
    sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8", errors="replace")

app = FastAPI(title="PlacePrep AI", version="1.0.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ── Routers ───────────────────────────────────────────────────────────────────
app.include_router(credits_router)
app.include_router(auth_router)
app.include_router(interview_router)
app.include_router(resume_router)

try:
    from api.credits_payment import router as payment_router
    app.include_router(payment_router)
except Exception as e:
    print(f"[WARN] Payment router not loaded: {e}")

try:
    from api.jd_upload import router as jd_router
    app.include_router(jd_router)
    print("[OK] JD upload router loaded")
except Exception as e:
    print(f"[WARN] JD router not loaded: {e}")

try:
    from api.officer import router as officer_router
    app.include_router(officer_router)
    print("[OK] Officer router loaded")
except Exception as e:
    print(f"[WARN] Officer router not loaded: {e}")

try:
    profile_router = import_module("api.profile_links").router
    app.include_router(profile_router)
    print("[OK] Profile links router loaded")
except Exception as e:
    print(f"[WARN] Profile links router not loaded: {e}")

try:
    from api.super_admin import router as super_router
    app.include_router(super_router)
    print("[OK] Super admin router loaded")
except Exception as e:
    print(f"[WARN] Super admin router not loaded: {e}")

# ── Startup ────────────────────────────────────────────────────────────────────
@app.on_event("startup")
async def startup():
    create_tables()
    print("[OK] All database tables created / verified")
    print("[OK] PlacePrep AI Backend Running")

@app.get("/health")
async def health():
    return {"status": "ok", "model": settings.GROQ_MODEL}

@app.get("/")
async def root():
    return {"status": "PlacePrep AI running"}
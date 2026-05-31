from dotenv import load_dotenv
import os

load_dotenv()

class Settings:
    GROQ_API_KEY: str    = os.getenv("GROQ_API_KEY", "")
    GROQ_API_KEY2: str   = os.getenv("GROQ_API_KEY2", "")
    GROQ_MODEL: str = "llama-3.3-70b-versatile"

    DATABASE_URL: str    = os.getenv("DATABASE_URL", "")
    REDIS_URL: str       = os.getenv("REDIS_URL", "redis://localhost:6379")

    JWT_SECRET: str      = os.getenv("JWT_SECRET", "secret")
    JWT_EXPIRE_HOURS: int = 24

    FRONTEND_URL: str    = os.getenv("FRONTEND_URL", "http://localhost:5173")

    MAX_QUESTIONS: int   = 12
    MIN_QUESTIONS: int   = 7
    MAX_MINUTES: int     = 22
    HARD_CUTOFF_MIN: int = 25
    MIN_MINUTES: int     = 10
    WARMUP_QUESTIONS: int = 2
    FEEDBACK_EVERY_N: int = 3

    POSTURE_WARNING_THRESHOLD: int = 50
    POSTURE_WARN_COOLDOWN_SEC: int = 60

    LOCAL_WHISPER_MODEL: str = "tiny"
    EMBEDDING_MODEL: str     = "all-MiniLM-L6-v2"
    TOP_K_JD_CHUNKS: int     = 3

    # ── Google OAuth ──────────────────────────────────────────────────────────
    GOOGLE_CLIENT_ID: str     = os.getenv("GOOGLE_CLIENT_ID", "")
    GOOGLE_CLIENT_SECRET: str = os.getenv("GOOGLE_CLIENT_SECRET", "")
    GOOGLE_REDIRECT_URI: str  = os.getenv(
        "GOOGLE_REDIRECT_URI",
        "http://localhost:8000/api/auth/google/callback"
    )

    # ── Supabase (optional — used alongside DATABASE_URL) ─────────────────────
    SUPABASE_URL: str      = os.getenv("SUPABASE_URL", "")
    SUPABASE_ANON_KEY: str = os.getenv("SUPABASE_ANON_KEY", "")

    # ── Domain / role control ─────────────────────────────────────────────────
    ALLOWED_STUDENT_DOMAIN: str = os.getenv("ALLOWED_STUDENT_DOMAIN", "indoreinstitute.com")
    SUPER_ADMIN_EMAILS: list    = [e.strip() for e in os.getenv("SUPER_ADMIN_EMAILS", "adityachouksey017@gmail.com,darpannaganpuriya@gmail.com").split(",")]

settings = Settings()
"""
api/auth.py  ─ Authentication: email/password + Google OAuth
Existing /register and /login routes kept intact.
New routes added: /api/auth/google  and  /api/auth/google/callback
Domain validation added to /register.
"""

import uuid, hashlib
import jwt as pyjwt
from datetime import datetime, timedelta

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import RedirectResponse
from sqlalchemy.orm import Session
from sqlalchemy import text
import httpx

from models.database import get_db, Student, College
from core.config import settings

router = APIRouter()


# ── Helpers ───────────────────────────────────────────────────────────────────

def make_token(student_id: str, role: str) -> str:
    payload = {
        "sub": student_id,
        "role": role,
        "exp": datetime.utcnow() + timedelta(hours=settings.JWT_EXPIRE_HOURS)
    }
    return pyjwt.encode(payload, settings.JWT_SECRET, algorithm="HS256")


def hash_password(password: str) -> str:
    return hashlib.sha256(password.encode()).hexdigest()


def _ensure_credits(db: Session, student_id: str, initial: int = 5) -> None:
    """Create a student_credits row if one doesn't exist yet."""
    try:
        existing = db.execute(
            text("SELECT id FROM student_credits WHERE student_id = :sid"),
            {"sid": student_id}
        ).fetchone()
        if not existing:
            db.execute(
                text("""
                    INSERT INTO student_credits (id, student_id, credits, total_used)
                    VALUES (:id, :sid, :credits, 0)
                """),
                {"id": str(uuid.uuid4()), "sid": student_id, "credits": initial}
            )
            db.commit()
    except Exception as e:
        print(f"⚠️ Could not ensure credits for {student_id[:8]}: {e}")


# ── Register ─────────────────────────────────────────────────────────────────

@router.post("/api/auth/register")
async def register(body: dict, db: Session = Depends(get_db)):
    email = body.get("email", "").strip().lower()
    super_admin_emails = settings.SUPER_ADMIN_EMAILS

    # Super admin always bypasses domain check
    credit_limit = 5
    if email not in super_admin_emails:
        email_domain = email.split("@")[-1] if "@" in email else ""

        # Check against enrolled colleges first
        enrolled_college = db.query(College).filter(
            College.domain == email_domain,
            College.is_active == True
        ).first()

        if not enrolled_college:
            raise HTTPException(
                status_code=400,
                detail="Your institution is not registered. Contact your placement officer."
            )
        credit_limit = enrolled_college.credit_limit if hasattr(enrolled_college, 'credit_limit') else 5

    existing = db.query(Student).filter(Student.email == email).first()
    if existing:
        raise HTTPException(400, "Email already registered")

    role = "super_admin" if email in super_admin_emails else body.get("role", "student")

    student = Student(
        name=body["name"],
        email=email,
        role=role,
        cgpa=body.get("cgpa", 7.0),
        branch=body.get("branch", "Computer Science"),
        password_hash=hash_password(body["password"])
    )
    db.add(student)
    db.commit()
    db.refresh(student)

    _ensure_credits(db, str(student.id), initial=credit_limit)

    token = make_token(str(student.id), student.role)
    return {
        "token": token,
        "role": student.role,
        "name": student.name,
        "id": str(student.id)
    }


# ── Login ─────────────────────────────────────────────────────────────────────

@router.post("/api/auth/login")
async def login(body: dict, db: Session = Depends(get_db)):
    student = db.query(Student).filter(Student.email == body["email"]).first()
    if not student:
        raise HTTPException(401, "Invalid email or password")
    if student.password_hash != hash_password(body["password"]):
        raise HTTPException(401, "Invalid email or password")

    # Always enforce super_admin role if this is the configured admin email
    if student.email in settings.SUPER_ADMIN_EMAILS:
        student_role = "super_admin"
        if student.role != "super_admin":
            student.role = "super_admin"
            db.commit()
    else:
        student_role = getattr(student, "role", "student") or "student"

    token = make_token(str(student.id), student_role)
    return {
        "token": token,
        "role": student_role,
        "name": student.name,
        "id": str(student.id)
    }


# ── get_current_user (shared JWT dependency) ──────────────────────────────────

def get_current_user(request: Request) -> dict:
    """Decode the Bearer JWT and return the payload dict {sub, role}."""
    auth = request.headers.get("Authorization", "")
    if not auth.startswith("Bearer "):
        raise HTTPException(401, "Missing or invalid token")
    token = auth.split(" ", 1)[1]
    try:
        payload = pyjwt.decode(token, settings.JWT_SECRET, algorithms=["HS256"])
        return payload
    except pyjwt.PyJWTError:
        raise HTTPException(401, "Invalid or expired token")


# ── Me (stub) ─────────────────────────────────────────────────────────────────

@router.get("/api/auth/me")
async def get_me(request: Request, db: Session = Depends(get_db)):
    auth_header = request.headers.get("Authorization")
    if not auth_header or not auth_header.startswith("Bearer "):
        raise HTTPException(401, "Missing or invalid token")
    token = auth_header.split(" ")[1]
    try:
        payload = pyjwt.decode(token, settings.JWT_SECRET, algorithms=["HS256"])
        student_id = payload.get("sub")
        if not student_id:
            raise HTTPException(401, "Invalid token payload")
    except pyjwt.PyJWTError:
        raise HTTPException(401, "Invalid token")
    
    student = db.query(Student).filter(Student.id == student_id).first()
    if not student:
        raise HTTPException(404, "Student not found")
        
    credits_record = db.execute(
        text("SELECT credits, total_used FROM student_credits WHERE student_id = :sid"),
        {"sid": student_id}
    ).fetchone()
    
    return {
        "name": student.name,
        "email": student.email,
        "role": student.role,
        "branch": student.branch,
        "cgpa": student.cgpa,
        "credits": credits_record.credits if credits_record else 0,
        "total_used": credits_record.total_used if credits_record else 0
    }


# ── Google OAuth ─────────────────────────────────────────────────────────────

@router.get("/api/auth/google")
async def google_login():
    """Step 1: return the Google OAuth consent URL to the frontend."""
    if not settings.GOOGLE_CLIENT_ID:
        raise HTTPException(503, "Google OAuth not configured on this server.")

    google_auth_url = (
        "https://accounts.google.com/o/oauth2/auth"
        f"?client_id={settings.GOOGLE_CLIENT_ID}"
        f"&redirect_uri={settings.GOOGLE_REDIRECT_URI}"
        f"&response_type=code"
        f"&scope=openid email profile"
        f"&access_type=offline"
    )
    return {"auth_url": google_auth_url}


@router.get("/api/auth/google/callback")
async def google_callback(code: str, db: Session = Depends(get_db)):
    """Step 2: exchange code → token → upsert user → redirect to frontend."""
    if not settings.GOOGLE_CLIENT_ID:
        raise HTTPException(503, "Google OAuth not configured on this server.")

    # Exchange authorisation code for access token
    async with httpx.AsyncClient() as client:
        token_resp = await client.post(
            "https://oauth2.googleapis.com/token",
            data={
                "code":          code,
                "client_id":     settings.GOOGLE_CLIENT_ID,
                "client_secret": settings.GOOGLE_CLIENT_SECRET,
                "redirect_uri":  settings.GOOGLE_REDIRECT_URI,
                "grant_type":    "authorization_code",
            }
        )
        tokens = token_resp.json()

        if "error" in tokens:
            return RedirectResponse(
                url=f"{settings.FRONTEND_URL}/login?error=google_token_failed"
            )

        # Fetch Google user profile
        user_resp = await client.get(
            "https://www.googleapis.com/oauth2/v2/userinfo",
            headers={"Authorization": f"Bearer {tokens['access_token']}"}
        )
        user_info = user_resp.json()

    email = user_info.get("email", "")
    name  = user_info.get("name", "")

    allowed_domain   = settings.ALLOWED_STUDENT_DOMAIN
    super_admin_emails = settings.SUPER_ADMIN_EMAILS

    is_super_admin = email in super_admin_emails

    # Domain gate (super-admin always passes)
    if not is_super_admin and not email.endswith(f"@{allowed_domain}"):
        return RedirectResponse(
            url=f"{settings.FRONTEND_URL}/login?error=domain_not_allowed"
        )

    # Find or create student record
    student = db.query(Student).filter(Student.email == email).first()
    if not student:
        role = "super_admin" if is_super_admin else "student"
        student = Student(
            id=str(uuid.uuid4()),
            name=name,
            email=email,
            password_hash="google_oauth",
            role=role,
        )
        db.add(student)
        db.commit()
        db.refresh(student)
        
        credit_limit = 5
        if not is_super_admin:
            email_domain = email.split("@")[-1] if "@" in email else ""
            enrolled_college = db.query(College).filter(
                College.domain == email_domain,
                College.is_active == True
            ).first()
            if enrolled_college:
                credit_limit = enrolled_college.credit_limit

        _ensure_credits(db, str(student.id), initial=credit_limit)
    else:
        # Promote to super_admin if the configured email signs in via Google
        if is_super_admin and student.role != "super_admin":
            student.role = "super_admin"
            db.commit()

    # Issue JWT
    token = make_token(str(student.id), student.role)

    # Redirect the browser to the frontend callback handler
    redirect_url = (
        f"{settings.FRONTEND_URL}/auth/callback"
        f"?token={token}"
        f"&role={student.role}"
        f"&name={name}"
        f"&id={student.id}"
    )
    return RedirectResponse(url=redirect_url)
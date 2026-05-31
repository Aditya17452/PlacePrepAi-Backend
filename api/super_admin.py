"""
api/super_admin.py — Super Admin control panel routes.
Only accessible by the configured SUPER_ADMIN_EMAIL account.
"""

import uuid
import hashlib
from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy.orm import Session
from sqlalchemy import text

from models.database import get_db, Student, InterviewSession, SessionScore, College
from core.config import settings
from api.auth import get_current_user

router = APIRouter(prefix="/api/super", tags=["super_admin"])

PLAN_CREDITS = {100: 500, 300: 1500, 600: 3000}


# ── Guard ─────────────────────────────────────────────────────────────────────

def require_super_admin(current_user: dict = Depends(get_current_user)):
    if current_user.get("role") != "super_admin":
        raise HTTPException(status_code=403, detail="Super admin access required")
    return current_user


# ── Platform stats ────────────────────────────────────────────────────────────

@router.get("/stats")
def get_platform_stats(
    db: Session = Depends(get_db),
    _: dict = Depends(require_super_admin)
):
    total_students = db.query(Student).filter(Student.role == "student").count()
    total_interviews = db.query(InterviewSession).count()
    completed = db.query(InterviewSession).filter(
        InterviewSession.status == "completed"
    ).count()
    total_colleges = db.query(College).filter(College.is_active == True).count()

    try:
        credits_data = db.execute(text(
            "SELECT COALESCE(SUM(credits), 0), COALESCE(SUM(total_used), 0) "
            "FROM student_credits"
        )).fetchone()
        total_credits = int(credits_data[0]) if credits_data else 0
        total_used    = int(credits_data[1]) if credits_data else 0
    except Exception:
        total_credits = 0
        total_used    = 0

    return {
        "total_students":          total_students,
        "total_interviews":        total_interviews,
        "completed_interviews":    completed,
        "total_colleges":          total_colleges,
        "total_credits_remaining": total_credits,
        "total_credits_consumed":  total_used,
    }


# ── All students ──────────────────────────────────────────────────────────────

@router.get("/students")
def get_all_students(
    db: Session = Depends(get_db),
    _: dict = Depends(require_super_admin)
):
    students = db.query(Student).filter(Student.role == "student").order_by(
        Student.created_at.desc()
    ).all()

    # Build college domain map for quick lookup
    colleges = {c.domain: c.name for c in db.query(College).all()}

    result = []
    for s in students:
        try:
            credits_row = db.execute(
                text("SELECT credits, total_used FROM student_credits WHERE student_id=:sid"),
                {"sid": str(s.id)}
            ).fetchone()
            credits = credits_row[0] if credits_row else 0
            used    = credits_row[1] if credits_row else 0
        except Exception:
            credits = 0
            used    = 0

        interview_count = db.query(InterviewSession).filter(
            InterviewSession.student_id == str(s.id),
            InterviewSession.status == "completed"
        ).count()

        scores = db.query(SessionScore).join(
            InterviewSession,
            SessionScore.session_id == InterviewSession.id
        ).filter(InterviewSession.student_id == str(s.id)).all()

        valid_scores = [sc.overall_score for sc in scores if sc.overall_score is not None]
        avg_score = round(sum(valid_scores) / len(valid_scores), 1) if valid_scores else 0

        # Derive college from email domain
        email_domain = s.email.split("@")[-1] if s.email and "@" in s.email else ""
        college_name = colleges.get(email_domain, "")

        result.append({
            "id":                str(s.id),
            "name":              s.name,
            "email":             s.email,
            "branch":            getattr(s, "branch", "") or "",
            "cgpa":              getattr(s, "cgpa", "") or "",
            "college":           college_name,
            "credits_remaining": credits,
            "credits_used":      used,
            "total_interviews":  interview_count,
            "avg_score":         avg_score,
        })

    return {"students": result}


# ── Credits management ────────────────────────────────────────────────────────

@router.post("/credits/{student_id}/add")
def super_add_credits(
    student_id: str,
    body: dict,
    db: Session = Depends(get_db),
    _: dict = Depends(require_super_admin)
):
    amount = int(body.get("amount", 5))
    if amount <= 0:
        raise HTTPException(400, "Amount must be positive")
    try:
        existing = db.execute(
            text("SELECT id FROM student_credits WHERE student_id=:sid"),
            {"sid": student_id}
        ).fetchone()
        if existing:
            db.execute(
                text("UPDATE student_credits SET credits = credits + :n WHERE student_id=:sid"),
                {"n": amount, "sid": student_id}
            )
        else:
            db.execute(
                text("INSERT INTO student_credits (id, student_id, credits, total_used) "
                     "VALUES (:id, :sid, :n, 0)"),
                {"id": str(uuid.uuid4()), "sid": student_id, "n": amount}
            )
        
        # Audit Log
        db.execute(text("""
            INSERT INTO credit_transactions
            (id, student_id, amount, reason, created_by, created_at)
            VALUES (:id, :sid, :n, 'admin_add', 'super_admin', NOW())
        """), {"id": str(uuid.uuid4()), "sid": student_id, "n": amount})
        
        db.commit()
        return {"success": True, "added": amount}
    except Exception as e:
        db.rollback()
        raise HTTPException(500, str(e))


@router.post("/credits/{student_id}/reset")
def super_reset_credits(
    student_id: str,
    body: dict,
    db: Session = Depends(get_db),
    _: dict = Depends(require_super_admin)
):
    amount = int(body.get("credits", 5))
    try:
        # Get existing credits to calculate diff for audit log
        existing = db.execute(
            text("SELECT credits FROM student_credits WHERE student_id=:sid"),
            {"sid": student_id}
        ).fetchone()
        
        db.execute(
            text("UPDATE student_credits SET credits=:n WHERE student_id=:sid"),
            {"n": amount, "sid": student_id}
        )
        
        # Audit Log
        db.execute(text("""
            INSERT INTO credit_transactions
            (id, student_id, amount, reason, created_by, created_at)
            VALUES (:id, :sid, :n, 'admin_reset', 'super_admin', NOW())
        """), {"id": str(uuid.uuid4()), "sid": student_id, "n": amount})

        db.commit()
        return {"success": True, "set_to": amount}
    except Exception as e:
        db.rollback()
        raise HTTPException(500, str(e))


# ── Student reports ───────────────────────────────────────────────────────────

@router.get("/student/{student_id}/reports")
def get_student_reports(
    student_id: str,
    db: Session = Depends(get_db),
    _: dict = Depends(require_super_admin)
):
    sessions = db.query(InterviewSession).filter(
        InterviewSession.student_id == student_id,
        InterviewSession.status == "completed"
    ).order_by(InterviewSession.started_at.desc()).all()

    result = []
    for s in sessions:
        score = db.query(SessionScore).filter(
            SessionScore.session_id == s.id
        ).first()
        result.append({
            "session_id":    str(s.id),
            "date":          s.started_at.strftime("%b %d, %Y") if s.started_at else "",
            "overall_score": score.overall_score if score else 0,
            "verdict":       score.verdict if score else "N/A",
            "technical":     score.technical_score if score else 0,
            "communication": score.communication_score if score else 0,
        })

    return {"reports": result}


@router.get("/credit-logs")
def get_credit_logs(
    db: Session = Depends(get_db), 
    _: dict = Depends(require_super_admin)
):
    rows = db.execute(text("""
        SELECT ct.student_id, s.name, ct.amount, 
               ct.reason, ct.created_at
        FROM credit_transactions ct
        LEFT JOIN students s ON s.id::text = ct.student_id
        ORDER BY ct.created_at DESC
        LIMIT 100
    """)).fetchall()
    return {"logs": [
        {
            "student_name": r[1] or r[0][:8],
            "amount": r[2],
            "reason": r[3],
            "date": str(r[4])[:10]
        } for r in rows
    ]}


# ── College management ────────────────────────────────────────────────────────

@router.get("/colleges")
def get_all_colleges(
    db: Session = Depends(get_db),
    _: dict = Depends(require_super_admin)
):
    colleges = db.query(College).order_by(College.created_at.desc()).all()
    result = []
    for c in colleges:
        # Count enrolled students from this domain
        enrolled = db.query(Student).filter(
            Student.role == "student",
            Student.email.like(f"%@{c.domain}")
        ).count()

        # Sum credits used by students from this college
        try:
            used_data = db.execute(text("""
                SELECT COALESCE(SUM(sc.total_used), 0)
                FROM student_credits sc
                JOIN students s ON s.id::text = sc.student_id
                WHERE s.email LIKE :domain AND s.role = 'student'
            """), {"domain": f"%@{c.domain}"}).fetchone()
            college_credits_used = int(used_data[0]) if used_data else 0
        except Exception:
            college_credits_used = 0

        result.append({
            "id":            str(c.id),
            "name":          c.name,
            "domain":        c.domain,
            "plan":          c.plan,
            "total_credits": c.total_credits,
            "credits_used":  college_credits_used,
            "credits_left":  max(0, c.total_credits - college_credits_used),
            "handler_email": c.handler_email or "",
            "is_active":     c.is_active,
            "enrolled_students": enrolled,
            "created_at":    c.created_at.strftime("%b %d, %Y") if c.created_at else "",
        })
    return {"colleges": result}


@router.post("/colleges/enroll")
def enroll_college(
    body: dict,
    db: Session = Depends(get_db),
    _: dict = Depends(require_super_admin)
):
    """
    Enroll a new college/institute.
    Creates the college record and a handler (officer) account.
    """
    name           = body.get("name", "").strip()
    domain         = body.get("domain", "").strip().lower().lstrip("@")
    plan           = int(body.get("plan", 100))
    handler_name   = body.get("handler_name", "").strip()
    handler_email  = body.get("handler_email", "").strip().lower()
    handler_password = body.get("handler_password", "")

    if not name or not domain or not handler_email or not handler_password:
        raise HTTPException(400, "name, domain, handler_email and handler_password are required")

    if plan not in PLAN_CREDITS:
        raise HTTPException(400, f"plan must be one of {list(PLAN_CREDITS.keys())}")

    # Check domain not already enrolled
    existing_college = db.query(College).filter(College.domain == domain).first()
    if existing_college:
        raise HTTPException(409, f"Domain '{domain}' is already enrolled")

    # Check handler email
    handler = db.query(Student).filter(Student.email == handler_email).first()
    
    total_credits = PLAN_CREDITS[plan]

    try:
        # 1. Create or update handler (officer) account
        if not handler:
            handler_id = str(uuid.uuid4())
            handler = Student(
                id=handler_id,
                name=handler_name or f"{name} Handler",
                email=handler_email,
                role="officer",
                password_hash=hashlib.sha256(handler_password.encode()).hexdigest(),
                branch="Administration",
                cgpa=0.0,
            )
            db.add(handler)
        else:
            handler.role = "officer"
            if handler_password:
                handler.password_hash = hashlib.sha256(handler_password.encode()).hexdigest()
            handler_id = handler.id
            
        db.flush() # flush to get handler inserted/updated in transaction

        # 2. Create college record
        college = College(
            id=str(uuid.uuid4()),
            name=name,
            domain=domain,
            plan=plan,
            total_credits=total_credits,
            credits_used=0,
            handler_email=handler_email,
            handler_id=handler_id,
            credit_limit=5,
            is_active=True,
        )
        db.add(college)

        # 3. Give handler minimal credits (they don't interview themselves)
        db.execute(text(
            "INSERT INTO student_credits (id, student_id, credits, total_used) VALUES (:id, :sid, 0, 0)"
        ), {"id": str(uuid.uuid4()), "sid": handler_id})

        db.commit()

        return {
            "success":       True,
            "college_id":    str(college.id),
            "handler_id":    handler_id,
            "plan":          plan,
            "total_credits": total_credits,
            "message":       f"College '{name}' enrolled. Handler account created for {handler_email}",
        }

    except Exception as e:
        db.rollback()
        raise HTTPException(500, str(e))


@router.post("/colleges/{college_id}/deactivate")
def deactivate_college(
    college_id: str,
    db: Session = Depends(get_db),
    _: dict = Depends(require_super_admin)
):
    college = db.query(College).filter(College.id == college_id).first()
    if not college:
        raise HTTPException(404, "College not found")
    
    db.execute(
        text("UPDATE colleges SET is_active=false WHERE id=:id"),
        {"id": college_id}
    )
    db.commit()
    return {"success": True, "message": f"College '{college.name}' deactivated"}


@router.get("/colleges/{college_id}/students")
def get_college_students(
    college_id: str,
    db: Session = Depends(get_db),
    _: dict = Depends(require_super_admin)
):
    """Get all students for a specific college."""
    college = db.query(College).filter(College.id == college_id).first()
    if not college:
        raise HTTPException(404, "College not found")

    students = db.query(Student).filter(
        Student.role == "student",
        Student.email.like(f"%@{college.domain}")
    ).all()

    result = []
    for s in students:
        try:
            cr = db.execute(
                text("SELECT credits, total_used FROM student_credits WHERE student_id=:sid"),
                {"sid": str(s.id)}
            ).fetchone()
            credits = cr[0] if cr else 0
            used    = cr[1] if cr else 0
        except Exception:
            credits = 0
            used    = 0

        interview_count = db.query(InterviewSession).filter(
            InterviewSession.student_id == str(s.id),
            InterviewSession.status == "completed"
        ).count()

        result.append({
            "id":                str(s.id),
            "name":              s.name,
            "email":             s.email,
            "branch":            s.branch or "",
            "cgpa":              s.cgpa or 0,
            "credits_remaining": credits,
            "credits_used":      used,
            "total_interviews":  interview_count,
        })

    return {"college": college.name, "domain": college.domain, "students": result}

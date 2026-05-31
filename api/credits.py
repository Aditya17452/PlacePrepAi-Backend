from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session
from sqlalchemy import text
from models.database import get_db, Student, College
from pydantic import BaseModel
import uuid
from datetime import datetime
from api.officer import require_officer, get_officer_college

router = APIRouter()


def _ensure_credits_table(db: Session):
    db.execute(text("""
        CREATE TABLE IF NOT EXISTS student_credits (
            id TEXT PRIMARY KEY,
            student_id TEXT UNIQUE NOT NULL,
            credits INTEGER DEFAULT 3,
            total_used INTEGER DEFAULT 0,
            updated_at TIMESTAMP DEFAULT NOW()
        )
    """))
    db.execute(text("""
        CREATE TABLE IF NOT EXISTS credit_transactions (
            id TEXT PRIMARY KEY,
            student_id TEXT NOT NULL,
            college_id TEXT,
            amount INTEGER NOT NULL,
            reason TEXT NOT NULL,
            created_by TEXT,
            created_at TIMESTAMP DEFAULT NOW()
        )
    """))
    db.commit()
    students = db.execute(text("SELECT id FROM students")).fetchall()
    for row in students:
        sid = row[0]
        exists = db.execute(
            text("SELECT id FROM student_credits WHERE student_id = :sid"),
            {"sid": sid}
        ).fetchone()
        if not exists:
            db.execute(
                text("INSERT INTO student_credits (id, student_id, credits, total_used, updated_at) VALUES (:id, :sid, 3, 0, NOW())"),
                {"id": str(uuid.uuid4()), "sid": sid}
            )
    db.commit()


@router.get("/api/credits/{student_id}")
def get_credits(student_id: str, db: Session = Depends(get_db)):
    try:
        _ensure_credits_table(db)
        row = db.execute(
            text("SELECT credits, total_used FROM student_credits WHERE student_id = :sid"),
            {"sid": student_id}
        ).fetchone()
        if not row:
            db.execute(
                text("INSERT INTO student_credits (id, student_id, credits, total_used, updated_at) VALUES (:id, :sid, 3, 0, NOW())"),
                {"id": str(uuid.uuid4()), "sid": student_id}
            )
            db.commit()
            return {"credits": 3, "total_used": 0}
        return {"credits": row[0], "total_used": row[1]}
    except Exception as e:
        print(f"get_credits error: {e}")
        return {"credits": 3, "total_used": 0}


@router.post("/api/credits/{student_id}/use")
def use_credit(student_id: str, db: Session = Depends(get_db)):
    try:
        student = db.query(Student).filter(Student.id == student_id).first()
        if not student:
            raise HTTPException(404, "Student not found")
        
        college = None
        if student.email and "@" in student.email:
            domain = student.email.split("@")[-1]
            college = db.query(College).filter(College.domain == domain).first()

        _ensure_credits_table(db)
        row = db.execute(
            text("SELECT credits, total_used FROM student_credits WHERE student_id = :sid"),
            {"sid": student_id}
        ).fetchone()

        if not row:
            init_credits = college.credit_limit if college else 5
            db.execute(
                text("INSERT INTO student_credits (id, student_id, credits, total_used, updated_at) VALUES (:id, :sid, :c, 0, NOW())"),
                {"id": str(uuid.uuid4()), "sid": student_id, "c": init_credits}
            )
            db.commit()
            row = (init_credits, 0)
            
        credits, used = row[0], row[1]

        if credits <= 0:
            raise HTTPException(
                status_code=403,
                detail="No credits remaining. Contact your placement officer."
            )
            
        if college and college.credits_used >= college.total_credits:
            raise HTTPException(
                status_code=403,
                detail="College master credit pool exhausted. Contact super admin."
            )

        db.execute(
            text("UPDATE student_credits SET credits = :c, total_used = :u, updated_at = NOW() WHERE student_id = :sid"),
            {"c": credits - 1, "u": used + 1, "sid": student_id}
        )
        
        if college:
            college.credits_used += 1
            
        # Audit Log
        db.execute(text("""
            INSERT INTO credit_transactions 
            (id, student_id, amount, reason, created_by, created_at)
            VALUES (:id, :sid, -1, 'interview_used', 'system', NOW())
        """), {"id": str(uuid.uuid4()), "sid": student_id})
        
        db.commit()
        print(f"✅ Credit used for {student_id[:8]}..., {credits - 1} remaining")
        return {"credits": credits - 1, "total_used": used + 1, "success": True}

    except HTTPException:
        raise
    except Exception as e:
        print(f"❌ use_credit error: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/api/officer/credits")
def list_all_credits(
    db: Session = Depends(get_db),
    current_user: dict = Depends(require_officer)
):
    try:
        officer = db.query(Student).filter(Student.id == current_user["sub"]).first()
        college = get_officer_college(db, officer.email)
        
        _ensure_credits_table(db)
        rows = db.execute(text("""
            SELECT s.id, s.name, s.email,
                   COALESCE(c.credits, :lim),
                   COALESCE(c.total_used, 0)
            FROM students s
            LEFT JOIN student_credits c ON s.id = c.student_id
            WHERE s.email LIKE :domain AND s.role = 'student'
            ORDER BY s.name
        """), {"domain": f"%@{college.domain}", "lim": college.credit_limit}).fetchall()
        
        students = [
            {"student_id": r[0], "name": r[1], "email": r[2], "credits": r[3], "total_used": r[4]}
            for r in rows
        ]
        return {
            "college": {
                "total_credits": college.total_credits,
                "credits_used": college.credits_used,
                "credit_limit": college.credit_limit
            },
            "students": students
        }
    except Exception as e:
        print(f"list_all_credits error: {e}")
        return {"college": {"total_credits": 0, "credits_used": 0, "credit_limit": 5}, "students": []}


@router.post("/api/officer/credits/{student_id}/add")
def add_credits(
    student_id: str, 
    body: dict, 
    db: Session = Depends(get_db),
    current_user: dict = Depends(require_officer)
):
    try:
        officer = db.query(Student).filter(Student.id == current_user["sub"]).first()
        college = get_officer_college(db, officer.email)
        
        if college.credits_used >= college.total_credits:
            raise HTTPException(status_code=403, detail="College credit pool exhausted")

        _ensure_credits_table(db)
        amount = int(body.get("amount", 1))
        if amount < 1 or amount > 20:
            raise HTTPException(status_code=400, detail="Amount must be 1–20")
            
        row = db.execute(
            text("SELECT credits FROM student_credits WHERE student_id = :sid"),
            {"sid": student_id}
        ).fetchone()
        
        if not row:
            new_credits = amount
            db.execute(
                text("INSERT INTO student_credits (id, student_id, credits, total_used, updated_at) VALUES (:id, :sid, :c, 0, NOW())"),
                {"id": str(uuid.uuid4()), "sid": student_id, "c": new_credits}
            )
        else:
            new_credits = row[0] + amount
            db.execute(
                text("UPDATE student_credits SET credits = :c, updated_at = NOW() WHERE student_id = :sid"),
                {"c": new_credits, "sid": student_id}
            )
            
        db.execute(text("""
            INSERT INTO credit_transactions
            (id, student_id, amount, reason, created_by, created_at)
            VALUES (:id, :sid, :n, 'officer_add', 'officer', NOW())
        """), {"id": str(uuid.uuid4()), "sid": student_id, "n": amount})
            
        db.commit()
        print(f"✅ Added {amount} credits to {student_id[:8]}..., now {new_credits}")
        return {"success": True, "new_credits": new_credits}
    except HTTPException:
        raise
    except Exception as e:
        print(f"add_credits error: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/api/officer/credits/{student_id}/reset")
def reset_credits(
    student_id: str, 
    body: dict, 
    db: Session = Depends(get_db),
    current_user: dict = Depends(require_officer)
):
    try:
        officer = db.query(Student).filter(Student.id == current_user["sub"]).first()
        college = get_officer_college(db, officer.email)
        
        if college.credits_used >= college.total_credits:
            raise HTTPException(status_code=403, detail="College credit pool exhausted")

        _ensure_credits_table(db)
        amount = int(body.get("credits", college.credit_limit))
        if amount < 0 or amount > 50:
            raise HTTPException(status_code=400, detail="Credits must be 0–50")
            
        row = db.execute(
            text("SELECT id FROM student_credits WHERE student_id = :sid"),
            {"sid": student_id}
        ).fetchone()
        
        if not row:
            db.execute(
                text("INSERT INTO student_credits (id, student_id, credits, total_used, updated_at) VALUES (:id, :sid, :c, 0, NOW())"),
                {"id": str(uuid.uuid4()), "sid": student_id, "c": amount}
            )
        else:
            db.execute(
                text("UPDATE student_credits SET credits = :c, updated_at = NOW() WHERE student_id = :sid"),
                {"c": amount, "sid": student_id}
            )
            
        db.execute(text("""
            INSERT INTO credit_transactions
            (id, student_id, amount, reason, created_by, created_at)
            VALUES (:id, :sid, :n, 'officer_reset', 'officer', NOW())
        """), {"id": str(uuid.uuid4()), "sid": student_id, "n": amount})
            
        db.commit()
        print(f"✅ Reset credits for {student_id[:8]}... to {amount}")
        return {"success": True, "credits": amount}
    except HTTPException:
        raise
    except Exception as e:
        print(f"reset_credits error: {e}")
        raise HTTPException(status_code=500, detail=str(e))


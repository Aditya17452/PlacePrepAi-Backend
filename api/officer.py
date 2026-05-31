from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session
from models.database import get_db, Student, InterviewSession, SessionScore, JobDescription, College
from agents.interview_agent import InterviewAgent
from core.config import settings
from api.auth import get_current_user

router = APIRouter(prefix="/api/officer", tags=["officer"])

def require_officer(current_user: dict = Depends(get_current_user)):
    if current_user.get("role") not in ["officer", "admin", "college_admin"]:
        raise HTTPException(status_code=403, detail="Officer access required")
    return current_user

def get_officer_college(db: Session, email: str) -> College:
    domain = email.split("@")[-1] if "@" in email else ""
    # Try finding an exact match for handler_email first
    college = db.query(College).filter(College.handler_email == email).first()
    if not college:
        # Fallback to domain match
        college = db.query(College).filter(College.domain == domain).first()
    if not college:
        raise HTTPException(status_code=404, detail="No college associated with this officer account")
    return college

@router.get("/me/college")
def get_my_college(
    db: Session = Depends(get_db),
    current_user: dict = Depends(require_officer)
):
    student = db.query(Student).filter(Student.id == current_user["sub"]).first()
    if not student:
        raise HTTPException(404, "User not found")
    
    college = get_officer_college(db, student.email)
    
    return {
        "id": str(college.id),
        "name": college.name,
        "domain": college.domain,
        "plan": college.plan,
        "total_credits": college.total_credits,
        "credits_used": college.credits_used
    }

@router.get("/students")
async def get_all_students(
    db: Session = Depends(get_db),
    current_user: dict = Depends(require_officer)
):
    """Get all students with their latest session scores."""
    officer = db.query(Student).filter(Student.id == current_user["sub"]).first()
    college = get_officer_college(db, officer.email)
    
    students = db.query(Student).filter(
        Student.role == "student",
        Student.email.like(f"%@{college.domain}")
    ).all()
    result = []
    
    for student in students:
        # Get latest session for this student
        latest_session = db.query(InterviewSession).filter(
            InterviewSession.student_id == str(student.id)
        ).order_by(InterviewSession.started_at.desc()).first()
        
        # Get latest scores for this student
        latest_scores = None
        if latest_session:
            latest_scores = db.query(SessionScore).filter(
                SessionScore.session_id == str(latest_session.id)
            ).first()

        jd = None
        if latest_session and latest_session.jd_id:
            jd = db.query(JobDescription).filter(JobDescription.id == latest_session.jd_id).first()
        
        student_data = {
            "id": str(student.id),
            "name": student.name,
            "email": student.email,
            "branch": student.branch,
            "cgpa": student.cgpa,
            "skills": student.skills,
            "latest_session": {
                "id": str(latest_session.id) if latest_session else None,
                "company": jd.company_name if jd else None,
                "role": jd.role_title if jd else None,
                "started_at": latest_session.started_at.isoformat() if latest_session and latest_session.started_at else None
            } if latest_session else None,
            "latest_scores": {
                "technical_score": latest_scores.technical_score if latest_scores else None,
                "communication_score": latest_scores.communication_score if latest_scores else None,
                "body_language_score": latest_scores.body_language_score if latest_scores else None,
                "overall_score": latest_scores.overall_score if latest_scores else None
            } if latest_scores else None
        }
        result.append(student_data)
    
    return {"students": result}

@router.post("/shortlist")
async def generate_shortlist(
    jd_id: str, 
    db: Session = Depends(get_db),
    current_user: dict = Depends(require_officer)
):
    """Generate AI shortlist for a specific JD."""
    officer = db.query(Student).filter(Student.id == current_user["sub"]).first()
    college = get_officer_college(db, officer.email)

    # Get JD details
    jd = db.query(JobDescription).filter(JobDescription.id == jd_id).first()
    if not jd:
        raise HTTPException(status_code=404, detail="Job description not found")
    
    # Get all students with their latest scores for this college
    students = db.query(Student).filter(
        Student.role == "student",
        Student.email.like(f"%@{college.domain}")
    ).all()
    student_list = []
    
    for student in students:
        latest_session = db.query(InterviewSession).filter(
            InterviewSession.student_id == str(student.id)
        ).order_by(InterviewSession.started_at.desc()).first()

        latest_scores = None
        if latest_session:
            latest_scores = db.query(SessionScore).filter(
                SessionScore.session_id == str(latest_session.id)
            ).first()
        
        student_data = {
            "id": str(student.id),
            "name": student.name,
            "cgpa": student.cgpa,
            "technical_score": latest_scores.technical_score if latest_scores else 0,
            "communication_score": latest_scores.communication_score if latest_scores else 0,
            "skills": student.skills
        }
        student_list.append(student_data)
    
    # Use InterviewAgent to generate shortlist
    agent = InterviewAgent(session_id="shortlist_temp", student={}, jd={
        "id": jd.id,
        "company_name": jd.company_name,
        "role_title": jd.role_title,
        "jd_text": jd.jd_text
    })
    
    ranked_candidates = await agent.generate_shortlist({
        "id": jd.id,
        "company_name": jd.company_name,
        "role_title": jd.role_title,
        "jd_text": jd.jd_text
    }, student_list)
    
    # Format response
    result = []
    for candidate in ranked_candidates:
        student = next((s for s in student_list if s["id"] == candidate["student_id"]), None)
        if student:
            result.append({
                "rank": candidate["rank"],
                "student_id": candidate["student_id"],
                "name": student["name"],
                "match_score": candidate["match_score"],
                "cgpa": student["cgpa"],
                "reasoning": candidate["reasoning"],
                "technical_score": student["technical_score"],
                "communication_score": student["communication_score"]
            })
    
    return {"jd_id": jd_id, "company": jd.company_name, "role": jd.role_title, "candidates": result}
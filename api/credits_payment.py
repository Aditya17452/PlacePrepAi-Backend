# backend/api/credits_payment.py
# Add this new file to your backend
# Install: pip install razorpay

from fastapi import APIRouter, HTTPException, Depends
from sqlalchemy.orm import Session
from sqlalchemy import text
from models.database import SessionLocal
from pydantic import BaseModel
import razorpay
import hmac
import hashlib
import os

router = APIRouter(prefix="/api/payments", tags=["payments"])

RAZORPAY_KEY_ID = os.getenv("RAZORPAY_KEY_ID", "rzp_test_YOUR_KEY_HERE")
RAZORPAY_KEY_SECRET = os.getenv("RAZORPAY_KEY_SECRET", "YOUR_SECRET_HERE")

# Credit packages
PACKAGES = {
    "basic":    {"credits": 5,  "amount": 4900,  "label": "5 Credits"},   # ₹49
    "standard": {"credits": 10, "amount": 8900,  "label": "10 Credits"},  # ₹89
    "pro":      {"credits": 20, "amount": 14900, "label": "20 Credits"},  # ₹149
}

def get_db():
    db = SessionLocal()
    try: yield db
    finally: db.close()

class CreateOrderRequest(BaseModel):
    student_id: str
    package: str  # basic | standard | pro

class VerifyPaymentRequest(BaseModel):
    student_id: str
    package: str
    razorpay_order_id: str
    razorpay_payment_id: str
    razorpay_signature: str

@router.get("/packages")
def get_packages():
    return {"packages": PACKAGES}

@router.post("/create-order")
def create_order(req: CreateOrderRequest, db: Session = Depends(get_db)):
    if req.package not in PACKAGES:
        raise HTTPException(status_code=400, detail="Invalid package")

    pkg = PACKAGES[req.package]

    try:
        client = razorpay.Client(auth=(RAZORPAY_KEY_ID, RAZORPAY_KEY_SECRET))
        order = client.order.create({
            "amount": pkg["amount"],   # in paise
            "currency": "INR",
            "payment_capture": 1,
            "notes": {
                "student_id": req.student_id,
                "package": req.package,
                "credits": pkg["credits"]
            }
        })
        return {
            "order_id": order["id"],
            "amount": pkg["amount"],
            "currency": "INR",
            "key": RAZORPAY_KEY_ID,
            "package": pkg
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Razorpay error: {str(e)}")

@router.post("/verify")
def verify_payment(req: VerifyPaymentRequest, db: Session = Depends(get_db)):
    # Verify signature
    body = f"{req.razorpay_order_id}|{req.razorpay_payment_id}"
    expected = hmac.new(
        RAZORPAY_KEY_SECRET.encode("utf-8"),
        body.encode("utf-8"),
        hashlib.sha256
    ).hexdigest()

    if expected != req.razorpay_signature:
        raise HTTPException(status_code=400, detail="Invalid payment signature")

    pkg = PACKAGES[req.package]
    credits_to_add = pkg["credits"]

    # Add credits to student
    try:
        existing = db.execute(
            text("SELECT id, credits FROM student_credits WHERE student_id = :sid"),
            {"sid": req.student_id}
        ).fetchone()

        if existing:
            db.execute(
                text("UPDATE student_credits SET credits = credits + :n, total_used = total_used, updated_at = NOW() WHERE student_id = :sid"),
                {"n": credits_to_add, "sid": req.student_id}
            )
        else:
            db.execute(
                text("INSERT INTO student_credits (id, student_id, credits, total_used) VALUES (gen_random_uuid(), :sid, :n, 0)"),
                {"sid": req.student_id, "n": credits_to_add}
            )

        # Log payment
        db.execute(
            text("""INSERT INTO payment_logs (id, student_id, order_id, payment_id, package, credits_added, amount, status)
                    VALUES (lower(hex(randomblob(16))), :sid, :oid, :pid, :pkg, :cred, :amt, 'success')"""),
            {
                "sid": req.student_id,
                "oid": req.razorpay_order_id,
                "pid": req.razorpay_payment_id,
                "pkg": req.package,
                "cred": credits_to_add,
                "amt": pkg["amount"] / 100
            }
        )

        db.commit()
        return {"success": True, "credits_added": credits_to_add, "message": f"{credits_to_add} credits added successfully"}
    except Exception as e:
        db.rollback()
        raise HTTPException(status_code=500, detail=str(e))

@router.get("/history/{student_id}")
def payment_history(student_id: str, db: Session = Depends(get_db)):
    try:
        rows = db.execute(
            text("SELECT * FROM payment_logs WHERE student_id = :sid ORDER BY created_at DESC LIMIT 20"),
            {"sid": student_id}
        ).fetchall()
        return {"payments": [dict(r._mapping) for r in rows]}
    except:
        return {"payments": []}
# Add this route to your existing backend/api/credits.py file
# Paste it AFTER the existing routes at the bottom of the file

from pydantic import BaseModel

class AddCreditsRequest(BaseModel):
    amount: int = 1

@router.post("/{student_id}/add")
def add_credits(student_id: str, req: AddCreditsRequest, db: Session = Depends(get_db)):
    """Direct credit add — used by mock payment and admin"""
    try:
        existing = db.execute(
            text("SELECT id FROM student_credits WHERE student_id = :sid"),
            {"sid": student_id}
        ).fetchone()

        if existing:
            db.execute(
                text("UPDATE student_credits SET credits = credits + :n WHERE student_id = :sid"),
                {"n": req.amount, "sid": student_id}
            )
        else:
            db.execute(
                text("INSERT INTO student_credits (id, student_id, credits, total_used) VALUES (lower(hex(randomblob(16))), :sid, :n, 0)"),
                {"sid": student_id, "n": req.amount}
            )
        db.commit()

        new_credits = db.execute(
            text("SELECT credits FROM student_credits WHERE student_id = :sid"),
            {"sid": student_id}
        ).fetchone()

        return {"success": True, "credits": new_credits[0] if new_credits else req.amount}
    except Exception as e:
        db.rollback()
        raise HTTPException(status_code=500, detail=str(e))    
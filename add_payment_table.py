# Run this ONCE to add payment_logs table to your existing database
# python add_payment_table.py

from models.database import SessionLocal
from sqlalchemy import text

db = SessionLocal()
try:
    db.execute(text("""
        CREATE TABLE IF NOT EXISTS payment_logs (
            id TEXT PRIMARY KEY,
            student_id TEXT NOT NULL,
            order_id TEXT,
            payment_id TEXT,
            package TEXT,
            credits_added INTEGER,
            amount FLOAT,
            status TEXT,
            created_at TIMESTAMP DEFAULT NOW()
        )
    """))
    db.commit()
    print("✅ payment_logs table created")
except Exception as e:
    print(f"❌ Error: {e}")
finally:
    db.close()
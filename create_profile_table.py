# Run ONCE: python create_profile_table.py
from models.database import SessionLocal
from sqlalchemy import text

db = SessionLocal()
try:
    db.execute(text("""
        CREATE TABLE IF NOT EXISTS student_profile_data (
            student_id TEXT PRIMARY KEY,
            scraped_summary TEXT,
            urls_found INTEGER DEFAULT 0,
            updated_at TIMESTAMP DEFAULT NOW()
        )
    """))
    db.commit()
    print("✅ student_profile_data table created")
except Exception as e:
    print(f"❌ {e}")
finally:
    db.close()

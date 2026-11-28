"""Engine, session factory and the declarative base.

The engine is configured from `app.core.config` with a small pool, because the app runs as
many short-lived Vercel functions behind the Supabase pooler rather than one long-lived
process.

There is no `create_tables()`. Schema changes go through Alembic; the app issues no DDL at
import or request time.

Table classes live in the sibling domain modules and are registered by
`app/models/__init__.py`, which imports them all — that is what Alembic autogenerate and
the test fixtures rely on.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

from sqlalchemy import create_engine
from sqlalchemy.orm import declarative_base, sessionmaker

from app.core.config import settings

_url = settings.sqlalchemy_url or "sqlite:///./placeprep.db"
_is_sqlite = _url.startswith("sqlite")

if _is_sqlite:
    engine = create_engine(_url, connect_args={"check_same_thread": False})
else:
    engine = create_engine(
        _url,
        pool_pre_ping=True,
        pool_size=settings.DB_POOL_SIZE,
        max_overflow=settings.DB_MAX_OVERFLOW,
        pool_recycle=settings.DB_POOL_RECYCLE_SEC,
        connect_args={
            # Never let one bad query hold a pooler connection open indefinitely.
            "options": f"-c statement_timeout={settings.DB_STATEMENT_TIMEOUT_MS}",
            "application_name": f"placeprep-{settings.ENV}",
        },
    )

SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
Base = declarative_base()


def gen_uuid() -> str:
    return str(uuid.uuid4())


def utcnow() -> datetime:
    """Timezone-aware UTC now, returned naive.

    The existing columns are `timestamp without time zone`; we keep writing naive UTC so
    the rows already stored stay comparable.
    """
    return datetime.now(UTC).replace(tzinfo=None)

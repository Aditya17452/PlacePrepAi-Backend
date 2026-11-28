"""Proctoring events and summaries (PLAN.md §4 Phase 4).

Events are what the browser reported; the summary is what the *server* concluded. Keeping
them apart is the point — the integrity score can be recomputed from the events at any
time, and a client that lies about severity cannot change the weighting.

Snapshots are stored only for violations, in a private Supabase Storage bucket, and
`snapshot_path` holds the object key rather than any image data. Retention is 14 days
(PLAN.md §0), enforced by a purge job.
"""

from __future__ import annotations

from sqlalchemy import (
    JSON,
    Boolean,
    CheckConstraint,
    Column,
    DateTime,
    Float,
    Index,
    Integer,
    String,
)

from models.database import Base, gen_uuid, utcnow


class ProctoringEvent(Base):
    __tablename__ = "proctoring_events"

    id = Column(String(36), primary_key=True, default=gen_uuid)
    session_id = Column(String(36), nullable=False, index=True)
    #: tab_switch | window_blur | fullscreen_exit | no_face | multiple_faces |
    #: gaze_away | face_mismatch | paste_attempt | devtools_open | phone_detected |
    #: second_voice | mic_muted
    type = Column(String(40), nullable=False)
    #: What the client claimed, 1-5. Advisory only; server weights dominate.
    severity = Column(Integer, nullable=False, default=1)
    detail = Column(JSON, nullable=True)
    #: The client's clock, kept for ordering within a batch but never trusted.
    client_ts = Column(DateTime, nullable=True)
    server_ts = Column(DateTime, nullable=False, default=utcnow)
    #: Storage object key, not image bytes.
    snapshot_path = Column(String(300), nullable=True)
    purged_at = Column(DateTime, nullable=True)

    __table_args__ = (
        CheckConstraint("severity between 1 and 5", name="ck_proctoring_severity"),
        Index("ix_proctoring_events_session_ts", "session_id", "server_ts"),
        Index("ix_proctoring_events_type", "type"),
    )


class ProctoringSummary(Base):
    __tablename__ = "proctoring_summaries"

    id = Column(String(36), primary_key=True, default=gen_uuid)
    session_id = Column(String(36), nullable=False, unique=True)
    integrity_score = Column(Float, nullable=True)
    #: clean | minor_flags | review_recommended
    verdict = Column(String(30), nullable=True)
    timeline = Column(JSON, nullable=True)
    auto_terminated = Column(Boolean, default=False, nullable=False)
    computed_at = Column(DateTime, nullable=True)

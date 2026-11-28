"""Background job queue and its handlers.

Importing this package registers every handler with the queue, which is why
`app/main.py` imports it. Handlers used to live as `@jobs.register` functions inside the
routers, where they were easy to miss and impossible to run without the HTTP layer.
"""

from __future__ import annotations

from app.workers import evaluate_session, match_student_jd  # noqa: F401
from app.workers.jobs import drain, enqueue, register, requeue_stale

__all__ = ["drain", "enqueue", "register", "requeue_stale"]

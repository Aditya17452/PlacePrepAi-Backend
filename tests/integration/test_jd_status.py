"""GET /api/jd/{id} must finish the work it is being polled about.

The client polls this endpoint until `status` leaves "pending". Nothing else runs the
match job between cron ticks, and on Vercel Hobby cron fires once a day — so without an
inline drain the client polls for up to 24 hours. Production showed exactly that: 1110+
requests against one JD id in under an hour, every one of them 200 and "pending".
"""

from __future__ import annotations

import pytest

from app.models import StudentJD
from app.workers import jobs
from tests.conftest import auth_header, make_student


def _pending_jd(db, student) -> StudentJD:
    row = StudentJD(
        student_id=str(student.id),
        company_name="Acme",
        role_title="Backend Engineer",
        jd_text="Python, FastAPI, Postgres. " * 20,
        status="pending",
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return row


@pytest.mark.integration
def test_polling_a_pending_jd_runs_its_own_match_job(client, db, monkeypatch):
    student = make_student(db, email="jdpoll@example.com")
    row = _pending_jd(db, student)

    drained: list[str] = []

    async def fake_drain(session, *, dedupe_key: str) -> bool:
        drained.append(dedupe_key)
        target = session.query(StudentJD).filter(StudentJD.id == row.id).first()
        target.status = "ready"
        session.commit()
        return True

    monkeypatch.setattr(jobs, "drain_one", fake_drain)

    response = client.get(f"/api/jd/{row.id}", headers=auth_header(student))

    assert response.status_code == 200, response.text
    # The job is claimed by this JD's own dedupe key, not by a blind queue sweep.
    assert drained == [f"match_jd:{row.id}"]
    # And the caller sees the finished state, not the stale row it was polling.
    assert response.json()["status"] == "ready"


@pytest.mark.integration
def test_a_jd_that_is_already_done_does_not_drain_again(client, db, monkeypatch):
    student = make_student(db, email="jddone@example.com")
    row = _pending_jd(db, student)
    row.status = "ready"
    db.commit()

    called = False

    async def fake_drain(session, *, dedupe_key: str) -> bool:
        nonlocal called
        called = True
        return False

    monkeypatch.setattr(jobs, "drain_one", fake_drain)

    response = client.get(f"/api/jd/{row.id}", headers=auth_header(student))

    assert response.status_code == 200
    assert called is False, "a finished JD must not keep claiming jobs on every poll"

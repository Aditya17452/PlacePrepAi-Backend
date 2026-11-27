"""One-time admin bootstrap.

`core/config.py` used to carry two Gmail addresses as a *default* value for
`SUPER_ADMIN_EMAILS`, committed to a public repository — so anyone who read the source
knew which accounts to go after, and anyone who could register with one of those
addresses became a super admin.

Now the list comes from the environment, is applied **once** to promote existing accounts,
and after that the database is the only authority on who is an admin.
"""

from __future__ import annotations

from sqlalchemy.orm import Session

from app.core import audit
from app.core.config import settings
from app.core.logging_setup import get_logger
from app.core.roles import Role, normalize_role
from app.models import Student

log = get_logger("bootstrap")


def bootstrap_admins(db: Session) -> list[str]:
    """Promote configured bootstrap emails to admin if they are not already.

    Returns the emails actually changed. Accounts that do not exist yet are skipped —
    we never create an account from configuration, because that would let a stale env var
    resurrect a removed admin.
    """
    emails = settings.admin_bootstrap_emails
    if not emails:
        return []

    promoted: list[str] = []
    for email in emails:
        student = db.query(Student).filter(Student.email == email).first()
        if student is None:
            log.info("admin_bootstrap_skipped_no_account", email=email)
            continue
        if normalize_role(student.role) is Role.ADMIN:
            continue

        previous_role = student.role
        student.role = Role.ADMIN.value
        # Force a re-login so the old session cannot keep its previous role assumptions.
        student.token_version = int(getattr(student, "token_version", 0) or 0) + 1
        audit.record(
            db,
            action="admin.bootstrap_promote",
            actor_id=None,
            actor_role="system",
            target=audit.target_for("student", str(student.id)),
            detail={"email": email, "from_role": previous_role},
        )
        promoted.append(email)

    if promoted:
        db.commit()
    return promoted

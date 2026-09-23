"""Session participation. Access itself is decided in `access.py` — nowhere else.

A `SessionParticipant` row is an EXPLICIT grant: the creator (as owner, in
`create_session`) and a member who joins a Slack thread they can already read
(`apps.slack.services`). Opening a chat never creates one — it used to, which
let any co-tenant turn a private conversation into a durable grant by
connecting a socket to its id. `tests/test_session_acl.py` pins the callers.
"""
from __future__ import annotations

from . import access
from .models import Session, SessionParticipant


def ensure_participant(session: Session, user, role: str = SessionParticipant.EDITOR) -> SessionParticipant:
    obj, _ = SessionParticipant.objects.get_or_create(
        session=session, user=user, defaults={"role": role}
    )
    return obj


def can_access(session: Session, user) -> bool:
    """Read access — `access.can_read`, under the name the socket used."""
    return access.can_read(user, session)


def role_for(session: Session, user) -> str | None:
    """Effective role — `access.role_for`."""
    return access.role_for(user, session)

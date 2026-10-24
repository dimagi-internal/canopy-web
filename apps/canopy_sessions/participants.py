"""Session participation. Access itself is decided in `access.py` — nowhere else.

A `SessionParticipant` row is an EXPLICIT grant: the creator (as owner, in
`create_session`) and a member who joins a Slack thread they can already read
(`apps.slack.services`). Opening a chat never creates one — it used to, which
let any co-tenant turn a private conversation into a durable grant by
connecting a socket to its id. `tests/test_session_acl.py` pins the callers.

Never implicitly into a SENSITIVE conversation (`harness.sensitivity`): being one
of its people is what `reads_content` admits, so a person joins one only by its
owner's browser act, which wraps the conversation's key to them
(`POST /{id}/participants`). `ensure_participant` adds nobody to one who is not its
creator and holds no wrap of its key — a Slack reply, a dispatch's asker or any
door to come.
"""
from __future__ import annotations

from apps.harness import sensitivity

from .models import Session, SessionKeyWrap, SessionParticipant


def may_join_implicitly(session: Session, user) -> bool:
    """Whether a door may add `user` to `session` without its owner's act: any
    ordinary conversation; a sensitive one only for its creator, or someone who
    already holds a wrap of its key (a dispatched or forked conversation carries
    its people's wraps, `conversation_keys.copy_key`)."""
    if not sensitivity.is_sensitive(session):
        return True
    if session.created_by_id is not None and session.created_by_id == user.pk:
        return True
    return SessionKeyWrap.objects.filter(session=session, user_id=user.pk).exists()


def ensure_participant(session: Session, user,
                       role: str = SessionParticipant.EDITOR) -> SessionParticipant | None:
    """`user`'s row on `session`, made if absent — or None, adding nobody, when
    `may_join_implicitly` says no."""
    if not may_join_implicitly(session, user):
        return None
    obj, _ = SessionParticipant.objects.get_or_create(
        session=session, user=user, defaults={"role": role}
    )
    return obj

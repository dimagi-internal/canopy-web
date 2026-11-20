"""Making a `BrainSource` for a session and keeping its participants true to the session.

Lazy by design: a source is made the first time anything asks about it (a person's consent
page, an exclusion, a read), and `refresh` re-reads the session's own rows. `manage.py
brain_backfill` makes every session's, in batches."""
from __future__ import annotations

from django.db import transaction

from . import participants
from .models import BrainSource, SourceParticipant


def for_session(session) -> BrainSource:
    """The source for `session`, made on first ask, with its participants current."""
    source, _ = BrainSource.objects.get_or_create(
        kind=BrainSource.SESSION, ref=str(session.pk),
        defaults={"session": session, "workspace_id": session.workspace_id})
    refresh(source, session)
    return source


def existing(session) -> BrainSource | None:
    return BrainSource.objects.filter(kind=BrainSource.SESSION, ref=str(session.pk)).first()


@transaction.atomic
def refresh(source: BrainSource, session=None) -> BrainSource:
    """Re-read the session: its workspace, why it is sealed (if it is), and who is in it. Never
    reopens a withdrawn source: a withdrawal is a person's act and only they undo it."""
    session = session if session is not None else source.session
    if session is None:
        return source
    reason = participants.sealed_reason(session)
    changed = []
    if source.workspace_id != session.workspace_id:
        source.workspace_id = session.workspace_id
        changed.append("workspace")
    if source.sealed_reason != reason:
        source.sealed_reason = reason
        changed.append("sealed_reason")
    if changed:
        source.save(update_fields=[*changed, "updated_at"])
    want = {(u, c): how for how, u, c in participants.people_of(session)}
    have = {(p.user_id, p.contact_id): p for p in source.participants.all()}
    for key, p in have.items():
        if key not in want:
            p.delete()
    for (u, c), how in want.items():
        if (u, c) not in have:
            SourceParticipant.objects.create(source=source, user_id=u, contact_id=c, how=how)
        elif have[(u, c)].how != how:
            SourceParticipant.objects.filter(pk=have[(u, c)].pk).update(how=how)
    return source

"""Making a `BrainSource` for a session and keeping its participants true to the session.

Lazy by design: a source is made the first time anything asks about it (a person's consent
page, an exclusion, a read), and `refresh` re-reads the session's own rows. It is re-read again
whenever someone is added to or taken out of the session, writes a line in it or starts a turn
in it (`signals.py`, `refresh_session`), and each time the associator looks at it. `manage.py
brain_backfill` makes every session's, in batches."""
from __future__ import annotations

from django.db import transaction

from apps.agents.models import Agent

from . import participants
from .models import BrainSource, SourceParticipant


def for_session(session) -> BrainSource:
    """The source for `session`, made on first ask, with its participants current."""
    source, _ = BrainSource.objects.get_or_create(
        kind=BrainSource.SESSION, ref=str(session.pk),
        defaults={"session": session, "workspace_id": session.workspace_id})
    refresh(source, session)
    return source


def refresh_session(session_id) -> BrainSource | None:
    """Re-read the participants of `session_id`'s source, if it has one (a session with none gets its
    source, current, the first time anything asks)."""
    source = (BrainSource.objects.filter(kind=BrainSource.SESSION, ref=str(session_id))
              .select_related("session").first())
    if source is None or source.session is None:
        return source
    return refresh(source, source.session)


def is_participant(session_id, *, user_id=None, contact_id=None) -> bool:
    """Is this person already a stored participant of the session's source? True when there is no
    source yet (nothing to refresh), so a hot path can skip the refresh cheaply."""
    source = (BrainSource.objects.filter(kind=BrainSource.SESSION, ref=str(session_id))
              .values_list("pk", flat=True).first())
    if source is None:
        return True
    rows = SourceParticipant.objects.filter(source_id=source)
    if user_id is None:
        return rows.filter(contact_id=contact_id).exists()
    # An agent's login is never a participant, so it never needs a refresh.
    return rows.filter(user_id=user_id).exists() or Agent.objects.filter(user_id=user_id).exists()


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
    # Everyone whose words are in it stays, even once taken out of it (`participants.people_of`); an
    # exclusion is its own row and outlives the excluder's participant row (`eligibility`).
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

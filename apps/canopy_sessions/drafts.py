"""Each person's outgoing draft in a session (spec 2026-09-26).

One open draft per (session, author). There is no lock: nobody waits for
anybody. The optimistic `version` survives only to reconcile one person's own
tabs (desktop + phone on the same account)."""
from __future__ import annotations

import datetime as dt

from django.db import transaction
from django.utils import timezone

from . import presence
from .models import Draft, Session

# How long an untouched draft still reads as someone typing. A body outlives
# the tab that typed it; past this it is a leftover, not a person mid-thought.
PEER_DRAFT_FRESH = dt.timedelta(minutes=10)


class DraftVersionMismatch(Exception):
    def __init__(self, current_version: int, current_body: str):
        self.current_version = current_version
        self.current_body = current_body
        super().__init__("draft version mismatch")


def draft_for(session: Session, user) -> Draft:
    draft, _ = Draft.objects.get_or_create(session=session, author=user, slot="next")
    return draft


def update_draft(session: Session, *, user, expected_version: int, body: str,
                 visibility: str | None = None) -> Draft:
    """`visibility`, when given and one of `Draft.VISIBILITY_CHOICES`, is stored
    alongside the body — the author's own choice of how much of this draft
    peers get to see. An old client never sends it (None), and an invalid
    value is silently ignored rather than raised: neither is a reason to fail
    the keystroke that carries it."""
    valid_visibility = {c for c, _ in Draft.VISIBILITY_CHOICES}
    with transaction.atomic():
        draft = Draft.objects.select_for_update().get(pk=draft_for(session, user).pk)
        if expected_version != draft.version:
            raise DraftVersionMismatch(draft.version, draft.body)
        draft.body = body
        draft.version += 1
        update_fields = ["body", "version", "updated_at"]
        if visibility is not None and visibility in valid_visibility:
            draft.visibility = visibility
            update_fields.append("visibility")
        draft.save(update_fields=update_fields)
    return draft


def commit_draft(session: Session, user) -> str:
    """Take MY draft's text and reset it. Returns the committed text."""
    with transaction.atomic():
        draft = Draft.objects.select_for_update().get(pk=draft_for(session, user).pk)
        text = draft.body
        draft.body = ""
        draft.version += 1
        draft.save(update_fields=["body", "version", "updated_at"])
    return text


def discard_draft(session: Session, user) -> Draft:
    return _clear(session, user)


def _clear(session, user) -> Draft:
    with transaction.atomic():
        draft = Draft.objects.select_for_update().get(pk=draft_for(session, user).pk)
        if draft.body:
            draft.body = ""
            draft.version += 1
            draft.save(update_fields=["body", "version", "updated_at"])
    return draft


def peer_drafts(session: Session, user=None) -> list[Draft]:
    """Other authors' drafts that are LIVE: non-empty, written by someone
    present in the session right now, and touched within `PEER_DRAFT_FRESH`.
    Oldest edit first. `user` None (a contact) excludes nobody.

    Both filters are needed. Presence alone keeps a box someone opened an hour
    ago and walked away from while the tab stayed open; freshness alone brings
    back a line sent over HTTP (which never cleared the server copy) the moment
    its author reconnects anywhere. Without either, `presence.left` cleared the
    row live and the next connect snapshot put it straight back.

    A `hidden` draft is excluded outright — its author chose to show peers
    nothing until send, and the snapshot (unlike a live `draft.typing` frame)
    has no DTO step to withhold it at, so the exclusion has to happen here.
    A `typing` draft still appears (the DTO blanks its body)."""
    present = presence.present_ids(session.id)
    if user is not None:
        present.discard(user.id)
    if not present:
        return []
    return list(
        Draft.objects.select_related("author")
        .filter(session=session, slot="next", author_id__in=present,
                updated_at__gte=timezone.now() - PEER_DRAFT_FRESH)
        .exclude(body="")
        .exclude(visibility=Draft.HIDDEN)
        .order_by("updated_at")
    )


def clear_after_http_send(session: Session, user, text: str) -> Draft | None:
    """An HTTP send (the socket's fallback) commits text the server draft may
    still hold. Clear it when it is what was sent — or the start of it, since the
    last keystroke frames are exactly what a dead socket loses — and return the
    cleared draft so the caller can tell the room. A draft holding something
    ELSE is the next line, typed in another tab while this send was in flight,
    and is left alone. None when there was nothing to clear."""
    sent = (text or "").strip()
    with transaction.atomic():
        draft = Draft.objects.select_for_update().filter(
            session=session, author=user, slot="next").first()
        if draft is None or not draft.body.strip() or not sent.startswith(draft.body.strip()):
            return None
        draft.body = ""
        draft.version += 1
        draft.save(update_fields=["body", "version", "updated_at"])
    draft.author = user
    return draft

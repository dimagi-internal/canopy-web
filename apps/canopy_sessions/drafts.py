"""Each person's outgoing draft in a session (spec 2026-09-26).

One open draft per (session, author). There is no lock: nobody waits for
anybody. The optimistic `version` survives only to reconcile one person's own
tabs (desktop + phone on the same account)."""
from __future__ import annotations

from django.db import transaction

from .models import Draft, Session


class DraftVersionMismatch(Exception):
    def __init__(self, current_version: int, current_body: str):
        self.current_version = current_version
        self.current_body = current_body
        super().__init__("draft version mismatch")


def draft_for(session: Session, user) -> Draft:
    draft, _ = Draft.objects.get_or_create(session=session, author=user, slot="next")
    return draft


def update_draft(session: Session, *, user, expected_version: int, body: str) -> Draft:
    with transaction.atomic():
        draft = Draft.objects.select_for_update().get(pk=draft_for(session, user).pk)
        if expected_version != draft.version:
            raise DraftVersionMismatch(draft.version, draft.body)
        draft.body = body
        draft.version += 1
        draft.save(update_fields=["body", "version", "updated_at"])
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


def peer_drafts(session: Session, user) -> list[Draft]:
    """Other authors' non-empty open drafts, oldest edit first."""
    return list(
        Draft.objects.select_related("author")
        .filter(session=session, slot="next").exclude(author=user).exclude(body="")
        .order_by("updated_at")
    )

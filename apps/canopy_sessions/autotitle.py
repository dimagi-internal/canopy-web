"""Server-side auto-title: the first assistant reply names an untitled session
after its first user message (single line, truncated). Replaces ace-web's
CLI-based auto-titler for embedded consumers; LLM titling is a later nicety.

Subscribes to the same `turn_events_appended` signal apps/realtime/signals.py
and apps/canopy_sessions/signals.py (Message projection) already consume — one
more independent receiver on the harness ledger's fan-out point, not a new
notification path.
"""
from __future__ import annotations

from django.db import transaction
from django.dispatch import receiver

from apps.harness.signals import turn_events_appended
from apps.realtime import groups

from .models import Message, Session

TITLE_MAX = 80


def maybe_autotitle(session_id) -> str | None:
    """Title an untitled session from its first user message. No-op (returns
    None) if the session already has a title, has no user message yet, or that
    message is blank. Publishes `session.title_updated` on the session's
    realtime group when it actually sets one."""
    with transaction.atomic():
        session = Session.objects.select_for_update().get(pk=session_id)
        if session.title:
            return None
        first = (
            Message.objects.filter(session=session, role=Message.USER)
            .order_by("turn_index")
            .first()
        )
        if first is None or not first.plaintext.strip():
            return None
        title = " ".join(first.plaintext.split())[:TITLE_MAX]
        session.title = title
        session.save(update_fields=["title"])
    groups.publish(
        groups.session_group(session.pk),
        {"type": "session.title_updated", "title": title},
    )
    return title


@receiver(turn_events_appended, dispatch_uid="chat_autotitle")
def on_turn_events(sender, turn, rows, **kwargs):
    """Signal receiver: an assistant event on a chat turn triggers the title check."""
    if not turn.chat_session_id:
        return
    if not any(row.kind == "assistant" for row in rows):
        return
    maybe_autotitle(turn.chat_session_id)

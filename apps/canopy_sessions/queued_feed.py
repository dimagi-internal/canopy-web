"""Push the WHOLE queued list to a session's watchers (spec 2026-09-26).

Whole, never a delta: a just-connected client has no correct prior to apply
a delta to (the status_feed rule). Never raises — the list is an enhancement
to a conversation that works without it."""
from __future__ import annotations

import logging

from apps.realtime.groups import publish, session_group

logger = logging.getLogger(__name__)


def publish_queued(session_id) -> None:
    from .models import Session
    from .services import queued_messages

    try:
        session = Session.objects.get(pk=session_id)
        payload = queued_messages(session)
    except Exception:  # noqa: BLE001
        logger.exception("could not derive queued list for %s", session_id)
        return
    publish(session_group(session_id), {"type": "session.queued", "queued": payload})

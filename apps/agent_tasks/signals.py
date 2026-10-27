"""The server's rail for "the agent responded too soon": a task's turn ended, and
the task is not done (`services.on_turn_ended` decides: nudge, or fail).

Every terminal transition of a turn writes a `status` row through the ledger's one
writer, which fires `turn_events_appended` after commit — so this sees a turn's end
however it got there (finish, cancel, a lease sweep), as the capability lease
release does. A close that skips its ledger line (an admission refusal) is left to
the task's deadline.
"""
from __future__ import annotations

import logging

from apps.harness.models import Turn
from apps.harness.signals import turn_events_appended

from . import services

logger = logging.getLogger(__name__)


def _on_events(sender, turn, rows, **kwargs):
    if not isinstance(turn.origin_ref, dict) or not turn.origin_ref.get("agent_task"):
        return
    if not any(getattr(r, "kind", "") == "status" and (r.payload or {}).get("status") in Turn.TERMINAL
               for r in rows):
        return
    try:
        turn.refresh_from_db()
        services.on_turn_ended(turn)
    except Exception:  # noqa: BLE001 — a turn's end must never fail on its task
        logger.exception("agent tasks: could not follow up turn %s", turn.pk)


turn_events_appended.connect(_on_events, dispatch_uid="agent_tasks_follow_up_on_turn_end")

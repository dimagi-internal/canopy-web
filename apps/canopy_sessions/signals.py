"""Project the harness ledger into chat Messages.

Subscribes to harness's turn_events_appended (fired post-commit, after bulk_create,
which emits no post_save) and materializes a session turn's assistant/tool events
into Message rows. Same signal SP1's realtime fan-out rides — chat is just a second,
independent consumer of it.
"""
from __future__ import annotations

from django.dispatch import receiver

from apps.harness.signals import (
    sessions_reported,
    turn_events_appended,
    turn_status_changed,
)


@receiver(turn_events_appended, dispatch_uid="chat_project_messages")
def _project_messages(sender, turn, rows, **kwargs):
    if not turn.chat_session_id:
        return
    from .services import project_events

    project_events(turn, rows)
    # The turn moved. Same signal, same reason as the Slack status line: a
    # `status` row IS the transition, and re-deriving is cheap next to the
    # append that just happened.
    if any(r.kind == "status" for r in rows):
        from .status_feed import publish_for_turn

        publish_for_turn(turn)


@receiver(turn_status_changed, dispatch_uid="chat_turn_status_enqueued")
def _status_on_enqueue(sender, turn, **kwargs):
    """The moment the ask exists. The one a person is actually looking at.

    Before this the first thing a chat page heard about a send was the reply —
    so a turn queued behind a closed laptop rendered as an empty panel with no
    way to tell it apart from one being worked on.
    """
    from .status_feed import publish_for_turn

    publish_for_turn(turn)


@receiver(sessions_reported, dispatch_uid="chat_turn_status_sweep")
def _status_sweep(sender, runner, **kwargs):
    """A runner died and cannot say so. Found on other runners' reports."""
    from .status_feed import sweep

    try:
        sweep()
    except Exception:  # noqa: BLE001 — never break a runner's report over a status
        import logging

        logging.getLogger(__name__).exception("turn-status sweep failed")

"""Relay agent replies to Slack — a consumer of the harness ledger signal.

Same hook `canopy_sessions/signals.py` rides to project replies into Messages,
and for the same reason: `append_events` bulk-creates, so there is no post_save
to listen to, and this signal fires post-commit.
"""
from __future__ import annotations

import logging

from django.dispatch import receiver

from apps.harness.signals import (
    session_menu_changed,
    sessions_reported,
    transcript_rows_streamed,
    turn_events_appended,
    turn_status_changed,
)

logger = logging.getLogger(__name__)


@receiver(turn_events_appended, dispatch_uid="slack_relay_replies")
def _relay_replies(sender, turn, rows, **kwargs):
    if not turn.chat_session_id:
        return
    from .relay import relay

    try:
        relay(turn, rows)
    except Exception:  # noqa: BLE001 — never break the runner's append over Slack
        logger.exception("slack relay failed")
    if any(r.kind == "status" for r in rows):
        from .status import on_status

        try:
            on_status(turn)
        except Exception:  # noqa: BLE001
            logger.exception("slack status line failed")


@receiver(turn_status_changed, dispatch_uid="slack_status_on_enqueue")
def _status_on_enqueue(sender, turn, **kwargs):
    """An ask now exists: give it its line in the thread.

    The same signal the chat feed rides, so Slack and the chat page hear about
    an ask at the same moment. `post` is a no-op for a session that was not
    born in Slack.
    """
    if not turn.chat_session_id:
        return
    from .status import on_status

    try:
        on_status(turn)
    except Exception:  # noqa: BLE001 — never break an enqueue over Slack
        logger.exception("slack status line failed on enqueue")


@receiver(transcript_rows_streamed, dispatch_uid="slack_transcript_rows")
def _transcript(sender, session, rows, **kwargs):
    from .relay import on_transcript

    try:
        on_transcript(session, rows)
    except Exception:  # noqa: BLE001 — never break the runner's stream over Slack
        logger.exception("slack transcript relay failed")


@receiver(session_menu_changed, dispatch_uid="slack_relay_menu")
def _relay_menu(sender, session_id, menu, **kwargs):
    from .relay import relay_menu

    try:
        relay_menu(session_id, menu)
    except Exception:  # noqa: BLE001
        logger.exception("slack menu relay failed")


@receiver(sessions_reported, dispatch_uid="slack_status_sweep")
def _status_sweep(sender, runner, **kwargs):
    # The clock for the one change nothing reports: a runner that died. Every
    # OTHER runner's ~10s report drives a throttled re-check (status.sweep).
    from .status import sweep

    try:
        sweep()
    except Exception:  # noqa: BLE001 — never break a runner's report over Slack
        logger.exception("slack status sweep failed")

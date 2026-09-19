"""Relay agent replies to Slack — a consumer of the harness ledger signal.

Same hook `canopy_sessions/signals.py` rides to project replies into Messages,
and for the same reason: `append_events` bulk-creates, so there is no post_save
to listen to, and this signal fires post-commit.
"""
from __future__ import annotations

import logging

from django.dispatch import receiver

from apps.harness.signals import session_menu_changed, transcript_user_rows, turn_events_appended

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


@receiver(transcript_user_rows, dispatch_uid="slack_elsewhere_notice")
def _elsewhere(sender, session, texts, **kwargs):
    from .relay import notify_elsewhere

    try:
        notify_elsewhere(session, texts)
    except Exception:  # noqa: BLE001
        logger.exception("slack elsewhere notice failed")


@receiver(session_menu_changed, dispatch_uid="slack_relay_menu")
def _relay_menu(sender, session_id, menu, **kwargs):
    from .relay import relay_menu

    try:
        relay_menu(session_id, menu)
    except Exception:  # noqa: BLE001
        logger.exception("slack menu relay failed")

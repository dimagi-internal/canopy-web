"""Relay agent replies to Slack — a consumer of the harness ledger signal.

Same hook `canopy_sessions/signals.py` rides to project replies into Messages,
and for the same reason: `append_events` bulk-creates, so there is no post_save
to listen to, and this signal fires post-commit.
"""
from __future__ import annotations

import logging

from django.dispatch import receiver

from apps.harness.signals import session_menu_changed, turn_events_appended

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


@receiver(session_menu_changed, dispatch_uid="slack_relay_menu")
def _relay_menu(sender, session_id, menu, **kwargs):
    from .relay import relay_menu

    try:
        relay_menu(session_id, menu)
    except Exception:  # noqa: BLE001
        logger.exception("slack menu relay failed")

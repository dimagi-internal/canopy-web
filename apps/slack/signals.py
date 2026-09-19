"""Relay agent replies to Slack — a consumer of the harness ledger signal.

Same hook `canopy_sessions/signals.py` rides to project replies into Messages,
and for the same reason: `append_events` bulk-creates, so there is no post_save
to listen to, and this signal fires post-commit.
"""
from __future__ import annotations

import logging

from django.dispatch import receiver

from apps.harness.signals import session_menu_changed, sessions_reported, turn_events_appended

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


# --- the status card (apps/slack/status.py) ---------------------------------------

@receiver(turn_events_appended, dispatch_uid="slack_status_on_turn")
def _status_on_turn(sender, turn, rows, **kwargs):
    # Only a status row moves the card; assistant/tool rows are the relay's.
    if not turn.chat_session_id or not any(r.kind == "status" for r in rows):
        return
    from .status import refresh

    refresh(turn.chat_session)


@receiver(session_menu_changed, dispatch_uid="slack_status_on_menu")
def _status_on_menu(sender, session_id, menu, **kwargs):
    from apps.canopy_sessions.models import Session

    from .status import refresh

    session = Session.objects.select_related("agent").filter(pk=session_id).first()
    if session is not None:
        refresh(session, create=False)


@receiver(sessions_reported, dispatch_uid="slack_status_sweep")
def _status_sweep(sender, runner, **kwargs):
    # The clock. A runner that died cannot report that it died, so every OTHER
    # runner's ~10s report drives a throttled pass that notices it.
    from .status import sweep

    try:
        sweep()
    except Exception:  # noqa: BLE001 — never break a runner's report over Slack
        logger.exception("slack status sweep failed")

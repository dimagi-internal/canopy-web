"""post_save receiver that marks an agent dirty when its waiting set may have
changed. Wiring only — whether to push is services.refresh_agent_waiting's call.

The waiting set is a single source: `AgentTask`, via `waiting_q()` — an open ask,
or a live task parked on a person. A task is a real row, so one receiver covers
everything — no per-producer hops, no Drive-backed staleness (the old gap when
run gates were projected from a RunStore), and nothing to keep in sync. The
schedule nag is an ask on a task too, so its raise/dismiss flows through here
for free. See 2026-07-21-supervisor-inbox-items-only-design.md.
"""
from __future__ import annotations

import logging

from django.db.models.signals import post_delete, post_save
from django.dispatch import receiver

from apps.agents.models import AgentTask

from .services import mark_dirty

logger = logging.getLogger(__name__)


@receiver([post_save, post_delete], sender=AgentTask)
def _task_changed(sender, instance: AgentTask, **kwargs) -> None:
    mark_dirty(instance.agent_id)  # the FK shadow attribute — no query


def _connect_transfer_requests():
    from apps.canopy_sessions.transfer_requests import transfer_request_changed

    @receiver(transfer_request_changed, dispatch_uid="push_transfer_request_approvers")
    def _ping_approvers(sender, request, **kwargs):
        """A request waiting on someone: push to every administrator of the target
        box, so the yes doesn't depend on them happening to look."""
        if request.status != "pending":
            return
        from apps.canopy_sessions.transfer_requests import approvers

        from .services import send_to_user, session_label

        who = getattr(request.requested_by, "email", "") or "Someone"
        body = (f"{who} wants to move “{session_label(request.session, 'a conversation')}” "
                f"onto {request.to_runner.name}.")
        url = f"/w/{request.session.workspace_id}/sessions/{request.session_id}"
        for user in approvers(request.to_runner):
            try:
                send_to_user(user, "Transfer request", body, url)
            except Exception:  # noqa: BLE001 — never break a request over push
                logger.exception("transfer-request push failed")


_connect_transfer_requests()


# --- chat sessions: the harness signals (apps/harness/signals.py) -------------
# Push listens; the harness never imports push.

def _connect_session_signals():
    from apps.harness.signals import (
        session_menu_changed,
        session_turn_enqueued,
        session_turn_finished,
    )

    @receiver(session_menu_changed, dispatch_uid="push_session_question")
    def _new_ask(sender, session_id, menu, new_ask=False, **kwargs):
        """To the phone in your pocket, for an agent that just STARTED waiting.
        Only a new ask: a retraction is not news, and a re-stamp of the same
        dialog is not a new question (it buzzed a phone every ten seconds,
        2026-09-22). A menu that renders perfectly still needs somebody to open
        the app."""
        if not new_ask or not menu:
            return
        from apps.canopy_sessions.models import Session

        from .services import notify_session_question

        session = (Session.objects.select_related("agent", "runner_binding",
                                                  "runner_binding__runner")
                   .filter(pk=session_id).first())
        if session is not None:
            notify_session_question(session, menu)

    @receiver(session_turn_enqueued, dispatch_uid="push_cancel_finish")
    def _new_work(sender, session_id, **kwargs):
        """New work on the session: it was not done, so drop any pending "done" push."""
        from .services import cancel_session_finish_push

        cancel_session_finish_push(session_id)

    @receiver(session_turn_finished, dispatch_uid="push_arm_finish")
    def _finished(sender, turn, **kwargs):
        """"Your chat is done" — arms the quiet-timer (or pushes now, if the chat
        asked for every completion); DONE/FAILED only, so a stop or a lost turn
        arms nothing."""
        from .services import on_session_turn_finished

        on_session_turn_finished(turn)


_connect_session_signals()

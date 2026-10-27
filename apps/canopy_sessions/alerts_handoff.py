"""Alert source `transfer_left_open`: a transferred session's task is still open
on the box it left (`handoff.py`).

Two ways that happens, both worth a person's eye:

* the close FAILED — the runner tried and emdash refused ("still in the sidebar
  after the delete"); it keeps retrying, and this says why it has not worked;
* the task was KEPT on purpose — the work could not be packed, or the target
  could not restore it exactly, or the conversation is sensitive — so that task
  holds the only copy of the work and canopy will not delete it unasked.

Before this a failed close sat in `close_error`, which nothing reads, and the
person archived the ghosts by hand (2026-10-23: 3 of 11). "Close it anyway"
forces the close; Dismiss is for a task someone already dealt with.
"""
from __future__ import annotations

import datetime as dt
import uuid

from django.db.models import Q

from apps.attention.alerts import DISMISS, Action, Raise
from apps.harness.runners import can_administer_runner
from apps.workspaces.links import scoped_path

from . import access
from .models import SessionHandoff

KIND = "transfer_left_open"
SEVERITY = "action"
CONFIRM_WITHIN = None
CLOSING_ACTION_IDS = ("close", "dismiss")
#: Older than this, a left-open task is history, not a to-do.
WINDOW = dt.timedelta(days=14)


def _left_open(now):
    return (SessionHandoff.objects
            .filter(created_at__gte=now - WINDOW)
            .filter(Q(close_status=SessionHandoff.KEPT)
                    | (Q(close_status=SessionHandoff.PENDING) & ~Q(close_error="")))
            .select_related("session", "source_runner", "target_runner"))


def reconcile(now) -> list[Raise]:
    out = []
    for h in _left_open(now):
        source = h.source_runner.name if h.source_runner_id else "the old runner"
        task = h.source_session_key or (h.session.title or "a session")
        failed = h.close_status == SessionHandoff.PENDING
        out.append(Raise(
            subject_key=f"handoff:{h.pk}",
            workspace=h.session.workspace_id,
            title=(f'Closing "{task}" on {source} after its transfer keeps failing' if failed
                   else f'"{task}" was left open on {source} after its transfer'),
            url=scoped_path(h.session.workspace_id, f"/sessions/{h.session_id}"),
            scopes=(f"workspace:{h.session.workspace_id}",
                    *([f"runner:{h.source_runner_id}"] if h.source_runner_id else []),
                    *([f"agent:{h.session.agent.slug}"] if h.session.agent_id else [])),
            body=h.close_error,
            fingerprint=f"{h.close_status}:{h.close_error}",
        ))
    return out


def _handoff_of(alert):
    kind, _, pk = alert.subject_key.partition(":")
    if kind != "handoff":
        return None
    try:
        pk = uuid.UUID(pk)
    except ValueError:
        return None
    return SessionHandoff.objects.filter(pk=pk).select_related("session", "source_runner").first()


def _may_act(user, h) -> bool:
    if h.source_runner_id and can_administer_runner(user, h.source_runner):
        return True
    return access.can_write(user, h.session)


def actions(alert, user) -> list[Action]:
    if not getattr(user, "is_authenticated", False):
        return []
    h = _handoff_of(alert)
    if h is None or not _may_act(user, h):
        return []
    label = "Retry close" if h.close_status == SessionHandoff.PENDING else "Close it anyway"
    return [Action("close", label, True), DISMISS]


def perform(alert, user, action_id) -> None:
    """`close` asks the source to close the task now, whatever it holds — the
    person has looked. The alert resolves once the runner reports it closed."""
    if action_id != "close":
        raise ValueError(f"{KIND} has no handler for {action_id!r}")
    h = _handoff_of(alert)
    if h is None or h.close_status not in (SessionHandoff.PENDING, SessionHandoff.KEPT):
        raise ValueError("That task is no longer open; nothing was changed.")
    h.close_status, h.close_forced, h.close_error = SessionHandoff.PENDING, True, ""
    h.save(update_fields=["close_status", "close_forced", "close_error"])

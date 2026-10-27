"""Alert source `runner_refresh`: a refresh someone asked for has not happened.

A Refresh is recorded (`Runner.refresh_requested_at`) and the box acts on it at
its next idle moment, within its floor between self-restarts
(`runner_refresh.status`). Most finish in minutes and their asker is told
(`runner_refresh.finished`). One still pending `STUCK_AFTER` after it was asked
is a problem: the box is dark, a turn has held it for half an hour, or its
bootstrap never reports back.

Handed to the person who asked (`handler_user`), so the hand-off tells them
once, by email and push. A refresh canopy asked for itself (a new agent's
setup) has no asker and is held by no one: it shows on the runner's page and to
its admins. It resolves itself when the box reports the bootstrap. `action`:
the box still runs turns, on what it had. Its one fix is the runner's page
(why it waits is there), plus Dismiss, for a box someone means to leave as it is.
Private runners (no workspace) are skipped: an alert needs a workspace to live in.
"""
from __future__ import annotations

import uuid
from datetime import timedelta

from apps.attention.alerts import DISMISS, Action, Raise

from .alerts_runner_credential import runner_path, watches
from .models import Runner
from .routing import agents_routed_to
from .runner_refresh import status
from .runners import can_administer_runner

KIND = "runner_refresh"
SEVERITY = "action"
CONFIRM_WITHIN = timedelta(hours=2)
CLOSING_ACTION_IDS = ("open", "dismiss")

STUCK_AFTER = timedelta(minutes=30)

_WHY = {
    "offline": "it is not heartbeating",
    "busy": "it is waiting for its turns in flight to finish",
    "floor": "it restarted itself recently and waits out its floor",
}


def _raise(runner, now) -> Raise:
    st = status(runner, now)
    minutes = int((now - runner.refresh_requested_at).total_seconds() // 60)
    why = [_WHY[w] + (f" ({st.in_flight})" if w == "busy" else "") for w in st.waiting_on]
    body = (f"Requested {minutes} min ago and still waiting: " + "; ".join(why) + "."
            if why else f"Requested {minutes} min ago. Nothing it reports is in the way, but it has not "
                        "reported a new bootstrap — check its log on that machine.")
    slugs = agents_routed_to(runner).values_list("slug", flat=True)
    return Raise(
        subject_key=f"{KIND}:{runner.pk}",
        workspace=runner.workspace_id,
        title=f"{runner.name}'s refresh has not run yet",
        url=runner_path(runner),
        scopes=(f"runner:{runner.pk}", f"workspace:{runner.workspace_id}", *[f"agent:{s}" for s in slugs]),
        body=body,
        # One request is one fact: the minutes ticking up are not news.
        fingerprint=runner.refresh_requested_at.isoformat(),
        handler_user=runner.refresh_requested_by_id,
        ask=f"You asked {runner.name} to refresh {minutes} min ago and it has not run yet.",
    )


def reconcile(now) -> list[Raise]:
    rows = (Runner.objects.exclude(status=Runner.RETIRED)
            .filter(workspace_id__isnull=False, refresh_requested_at__lt=now - STUCK_AFTER)
            .select_related("refresh_requested_by"))
    return [_raise(r, now) for r in rows if watches(r) and r.refresh_requested()]


def _runner_of(alert):
    prefix, _, pk = alert.subject_key.partition(":")
    if prefix != KIND:
        return None
    try:
        pk = uuid.UUID(pk)
    except ValueError:
        return None
    return Runner.objects.filter(pk=pk).select_related("owner").first()


def actions(alert, user) -> list[Action]:
    if not getattr(user, "is_authenticated", False):
        return []
    runner = _runner_of(alert)
    if runner is None or not can_administer_runner(user, runner):
        return []
    return [Action("open", "Open runner", True, href=runner_path(runner)), DISMISS]


def perform(alert, user, action_id) -> None:
    """`open` is a link and `dismiss` is the framework's; nothing runs here."""
    raise ValueError(f"runner_refresh has no handler for {action_id!r}")

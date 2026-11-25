"""Alert source `reply_latency`: people writing in over a host site are waiting too long.

The promise (`harness.latency.BUDGET_P95_SECONDS`): a contact turn on a warm
session gets its answer back to the site, p95, inside the budget. This source
checks the last 24 hours of each workspace's contact turns and raises one alert
per workspace whose p95 is over it (with at least `BUDGET_MIN_SAMPLE` turns,
so one slow turn is not a broken promise). It resolves itself when the window
is back under budget.

`action`: replies still arrive, only slowly. The alert names the slowest turn,
whose trace (`GET /api/harness/turns/{id}/latency`) says which hop took the time.
Fixing it is a code or runner change, so the one closing action is Dismiss.
"""
from __future__ import annotations

from datetime import timedelta

from apps.attention.alerts import DISMISS, Action, Raise
from apps.workspaces import permissions as perms

from .. import latency

KIND = "reply_latency"
SEVERITY = "action"
CONFIRM_WITHIN = timedelta(hours=2)
CLOSING_ACTION_IDS = ("dismiss",)


def _raise(breach: dict) -> Raise:
    ws = breach["workspace"]
    budget = latency.BUDGET_P95_SECONDS
    return Raise(
        subject_key=f"{KIND}:{ws}",
        workspace=ws,
        title=f"Replies to people over a site take {breach['p95']:.0f}s at p95 (budget {budget:.0f}s)",
        url=f"/api/harness/turns/{breach['slowest_turn']}/latency",
        scopes=(f"workspace:{ws}",),
        body=(f"{breach['n']} warm contact turns in the last 24 hours: p50 {breach['p50']:.1f}s, "
              f"p95 {breach['p95']:.1f}s, slowest {breach['slowest']:.1f}s (turn {breach['slowest_turn']}). "
              f"The slowest turn's trace says which hop took the time; "
              f"/api/harness/latency/contact has every hop's p50/p95."),
        # Rounded to the second so the alert's facts change when the wait does, not every tick.
        fingerprint=f"{ws}:{round(breach['p95'])}",
    )


def reconcile(now) -> list[Raise]:
    return [_raise(b) for b in latency.budget_breaches(now)]


def actions(alert, user) -> list[Action]:
    if not getattr(user, "is_authenticated", False):
        return []
    if not perms.can(user, alert.workspace_id, perms.AGENT_WORK):
        return []
    return [DISMISS]


def perform(alert, user, action_id) -> None:
    """`dismiss` is the framework's; nothing runs here."""
    raise ValueError(f"reply_latency has no handler for {action_id!r}")

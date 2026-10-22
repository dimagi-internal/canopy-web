"""Alert source `turn_failure`: a failed turn Ada could not handle.

A failure starts a debug turn of the conductor agent (`auto_debug`); its memory
is a `FailureInvestigation`. While that investigation is `open` it is Ada's:
still logged as an error, and it spawns NO alert. Once it is a person's —
`escalated` (the fix did not hold), `debugger_failed` (the debug turn itself
failed) or `held` (the fleet-wide rate cap said no) — it raises an `action`
alert in the investigation's workspace.

That workspace is where the debugger records the investigation, which may not
be the failing agent's own workspace. The alert lives there on purpose — a
deliberate deviation from the spec's Tenancy line ("the subject's workspace"):
`auto_debug.can_handle`, the only gate on its actions, is asked about that same
workspace, so an agent-workspace member who is not a log reader there would
see an alert with nothing to do on it. The agent and runners are still named
in its scopes, so their pages show it to those who can act.

The fingerprint is the investigation's `status` alone. A repeat of the same
failure (`auto_debug._add_occurrence` bumps `occurrences`, refreshes the
sample note) is not a new fact: a dismissed alert stays dismissed through
repeats, and reopens only when the status changes (e.g. `held` → `escalated`).

Resolves when the investigation leaves those statuses (a debugger or person
resolved it, or it was reopened for a debug turn). `mark_resolved` is
`auto_debug.resolve`, the same call the debugger's own endpoint makes; it
refuses (`ValueError`, a 409 from the API) when the investigation has already
left those statuses, so a stale alert cannot clobber a newer resolution or a
debug turn that took it back.

Offered only to people who may read and resolve investigations there
(`auto_debug.can_handle`: log readers and the debugger's admins), because the
note is turn content. There is no single-failure page, so "open" goes to the
agent's Turns tab (or the workspace activity log) where the failed turns are.

The title names the runners and the first sentence of the sample note, never an
investigation id or fingerprint.
"""
from __future__ import annotations

import re

from apps.agents.models import Agent
from apps.attention.alerts import DISMISS, Action, Raise
from apps.workspaces import services as wsvc

from . import auto_debug
from .models import FailureInvestigation, Runner

KIND = "turn_failure"
SEVERITY = "action"
CONFIRM_WITHIN = None
CLOSING_ACTION_IDS = ("mark_resolved", "dismiss")

#: A person's, not Ada's: never `open` (a debug turn has it) or `resolved`.
RAISING_STATUSES = (
    FailureInvestigation.ESCALATED,
    FailureInvestigation.DEBUGGER_FAILED,
    FailureInvestigation.HELD,
)

NOTE_MAX = 120
_SPACE = re.compile(r"\s+")
_SENTENCE_END = re.compile(r"(?<=[.!?])\s")


def _first_sentence(note: str) -> str:
    text = _SPACE.sub(" ", note or "").strip()
    first = _SENTENCE_END.split(text, maxsplit=1)[0]
    if len(first) > NOTE_MAX:
        first = first[: NOTE_MAX - 1].rstrip() + "…"
    return first or "no details recorded"


def title_for(inv) -> str:
    runners = ", ".join(inv.runners or []) or "the fleet"
    return f"Turns failing on {runners}: {_first_sentence(inv.sample_note)}"


def investigation_path(inv) -> str:
    """No page shows one investigation; the failed turns live on the agent's
    Turns tab, else the workspace's activity log. App-relative."""
    ws = inv.workspace_id
    slug = (inv.agents or [None])[0]
    if slug and Agent.objects.filter(slug=slug, workspace_id=ws).exists():
        return f"/w/{ws}/agents/{slug}/turns"
    return f"/w/{ws}/activity"


def _scopes(inv) -> tuple[str, ...]:
    tree = {inv.workspace_id} | wsvc.descendant_slugs({inv.workspace_id})
    runner_ids = []
    for name in inv.runners or []:  # names the investigation recorded; skip unknown
        runner = Runner.objects.filter(name=name, workspace_id__in=tree).order_by("pk").first()
        if runner is not None:
            runner_ids.append(runner.pk)
    agent_slugs = Agent.objects.filter(slug__in=inv.agents or [], workspace_id__in=tree) \
        .values_list("slug", flat=True)
    return (f"workspace:{inv.workspace_id}",
            *[f"runner:{pk}" for pk in runner_ids],
            *[f"agent:{s}" for s in agent_slugs])


def reconcile(now) -> list[Raise]:
    out: list[Raise] = []
    for inv in FailureInvestigation.objects.filter(status__in=RAISING_STATUSES):
        out.append(Raise(
            subject_key=f"investigation:{inv.pk}",
            workspace=inv.workspace_id,
            title=title_for(inv),
            url=investigation_path(inv),
            scopes=_scopes(inv),
            body=(inv.sample_note or "")[:1000],
            fingerprint=inv.status,  # not occurrences: a repeat is not a new fact
        ))
    return out


def _investigation_of(alert):
    kind, _, pk = alert.subject_key.partition(":")
    if kind != "investigation" or not pk.isdigit():
        return None
    return FailureInvestigation.objects.filter(pk=int(pk)).first()


def actions(alert, user) -> list[Action]:
    inv = _investigation_of(alert)
    if inv is None or not auto_debug.can_handle(user, alert.workspace_id):
        return []
    return [Action("open", "Open the investigation", False, href=investigation_path(inv)),
            Action("mark_resolved", "Mark resolved", True), DISMISS]


def perform(alert, user, action_id) -> None:
    if action_id != "mark_resolved":
        raise ValueError(f"turn_failure has no handler for {action_id!r}")
    inv = _investigation_of(alert)
    if inv is None:
        return  # gone: nothing left to resolve, and the alert closing is true
    if inv.status not in RAISING_STATUSES:
        raise ValueError(f"this investigation is no longer waiting on a person "
                         f"(it is {inv.get_status_display().lower()}); nothing was changed")
    auto_debug.resolve(inv, note="Resolved from the alert", by=user)

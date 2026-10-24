"""Alert source `turn_failure`: an unexpected turn failure, from the moment it is
recorded until it is fixed.

One lifecycle (Jonathan, 2026-10-23). A failure becomes a `FailureInvestigation`
(the memory: fingerprint, occurrences, fix commit, evidence) and, at once, this
alert (its face) — `auto_debug.on_turn_failed` raises it with `raise_now` before
the debug turn it starts, and the tick's `reconcile` below agrees. WHO HAS IT is
the alert's handler (`Alert.handler_agent` / `handler_user`):

- `open`, `held`, `debugger_failed`, `pending_deploy` — the DEBUGGER AGENT's.
  Visible on canopy-web, silent: nobody is notified while an agent works it, and
  a clean fix closes it without anyone hearing of it.
- `escalated` — a PERSON's (`inv.handler`, the one-line `inv.ask`): the debugger
  handed it off, or it came back on a runner that runs the fix. The hand-off is
  the one notification.
- `resolved` — no alert; the open one closes with the cause, PR and deploy state
  on its `resolved` event (`auto_debug.resolve`).

This used to raise only for the person statuses, and an `open` investigation
lived outside the alert model entirely, because an alert meant "a thing that
needs a person" and had no assignee. So the agent-owned stage could not be an
alert, the debugger's only verb was resolve, and resolving "needs Jonathan"
cleared the alert and re-armed the next escalation (investigation 10: debug
turns 2026-10-22 15:02 and 15:04, each emailing him).

That workspace is where the debugger records the investigation, which may not
be the failing agent's own workspace. The alert lives there on purpose — a
deliberate deviation from the spec's Tenancy line ("the subject's workspace"):
`auto_debug.can_handle`, the only gate on its actions, is asked about that same
workspace, so an agent-workspace member who is not a log reader there would
see an alert with nothing to do on it. The agent and runners are still named
in its scopes, so their pages show it to those who can act.

The fingerprint is the investigation's status and who has it. A repeat of the
same failure (`auto_debug._add_occurrence` bumps `occurrences`, refreshes the
sample note) is not a new fact: a dismissed alert stays dismissed through
repeats, and reopens when the status or the handler changes.

`mark_resolved` is `auto_debug.resolve`, by a person; it refuses (`ValueError`,
a 409 from the API) once the investigation is already resolved, so a stale
alert cannot clobber a newer resolution.

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
from apps.attention.alerts import Action, Raise
from apps.workspaces import services as wsvc

from . import auto_debug
from .models import FailureInvestigation, Runner

KIND = "turn_failure"
SEVERITY = "action"
CONFIRM_WITHIN = None
CLOSING_ACTION_IDS = ("mark_resolved",)

#: Every status but resolved is an open alert.
RAISING_STATUSES = (
    FailureInvestigation.OPEN,
    FailureInvestigation.HELD,
    FailureInvestigation.DEBUGGER_FAILED,
    FailureInvestigation.PENDING_DEPLOY,
    FailureInvestigation.ESCALATED,
)
#: Of those, the ones a person has.
PERSON_STATUSES = (FailureInvestigation.ESCALATED,)

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
    lead = {
        FailureInvestigation.PENDING_DEPLOY: f"Fix pending deploy on {runners}",
        FailureInvestigation.ESCALATED: f"Needs you — turns keep failing on {runners}",
    }.get(inv.status, f"Turns keep failing on {runners}")
    return f"{lead}: {_first_sentence(inv.sample_note)}"


def investigation_path(inv) -> str:
    """No page shows one investigation; the failed turns live on the agent's
    Turns tab, else the workspace's activity log. App-relative."""
    ws = inv.workspace_id
    slug = (inv.agents or [None])[0]
    if slug and Agent.objects.filter(slug=slug, workspace_id=ws).exists():
        return f"/w/{ws}/agents/{slug}/turns"
    return f"/w/{ws}/activity"


def subject_key(inv) -> str:
    return f"investigation:{inv.pk}"


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


def _handler(inv) -> tuple[int | None, int | None]:
    """(agent pk, user pk) — who has it. A person's row with no person recorded
    (the owner is gone) is unassigned: everyone who can close it sees it."""
    if inv.status in PERSON_STATUSES:
        return None, inv.handler_id
    debugger = auto_debug.debugger_agent()
    return (debugger.pk if debugger is not None else None), None


def raise_for(inv) -> Raise:
    agent_id, user_id = _handler(inv)
    body = (inv.sample_note or "")[:1000]
    if inv.status in PERSON_STATUSES and inv.ask:
        body = f"{inv.ask}\n\n{body}"
    return Raise(
        subject_key=subject_key(inv),
        workspace=inv.workspace_id,
        title=title_for(inv),
        url=investigation_path(inv),
        scopes=_scopes(inv),
        body=body,
        # Not occurrences: a repeat is not a new fact. Who has it is.
        fingerprint=f"{inv.status}:{agent_id or ''}:{user_id or ''}",
        handler_agent=agent_id,
        handler_user=user_id,
        ask=inv.ask if user_id else "",
    )


def reconcile(now) -> list[Raise]:
    auto_debug.close_live_fixes(now)
    return [raise_for(inv) for inv in FailureInvestigation.objects.filter(status__in=RAISING_STATUSES)]


def _investigation_of(alert):
    kind, _, pk = alert.subject_key.partition(":")
    if kind != "investigation" or not pk.isdigit():
        return None
    return FailureInvestigation.objects.filter(pk=int(pk)).first()


def actions(alert, user) -> list[Action]:
    inv = _investigation_of(alert)
    if inv is None or not auto_debug.can_handle(user, alert.workspace_id):
        return []
    return [Action("open", "See failed turns", False, href=investigation_path(inv)),
            Action("mark_resolved", "Mark resolved", True)]


def perform(alert, user, action_id) -> None:
    if action_id != "mark_resolved":
        raise ValueError(f"turn_failure has no handler for {action_id!r}")
    inv = _investigation_of(alert)
    if inv is None:
        return  # gone: nothing left to resolve, and the alert closing is true
    if inv.status not in RAISING_STATUSES:
        raise ValueError(f"this investigation is already "
                         f"{inv.get_status_display().lower()}; nothing was changed")
    # The framework closes the alert itself (reason=action) once this returns.
    auto_debug.resolve(inv, note="Resolved from the alert", by=user, close_alert=False)

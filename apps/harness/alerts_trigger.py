"""Alert source `trigger_stopped`: an agent's trigger that may no longer fire.

A trigger fires with its creator's authority on the agent, never more
(`triggers.refusal`). When that person leaves the workspace, is deactivated, or
loses the right the trigger needs (an agent admin for an inbound_mail or auto
trigger; `agent.work` for a cron one), every firing path refuses it — and this is
what makes that loud: one `action` alert per stopped trigger, on the agent's
Triggers tab, to whoever may set such a trigger up there.

It resolves when the condition goes: the creator's right returns, or the trigger
is paused or deleted. Editing does not take a trigger over (its creator stays), so
the fix is to restore that person's access or re-create the trigger as yourself.
"""
from __future__ import annotations

import hashlib

from apps.attention.alerts import Action, Raise

from . import triggers
from .models import AgentSchedule

KIND = "trigger_stopped"
SEVERITY = "action"
CONFIRM_WITHIN = None
CLOSING_ACTION_IDS = ("triggers",)


def triggers_path(workspace: str, slug: str) -> str:
    """The agent's Triggers tab. App-relative."""
    return f"/w/{workspace}/agents/{slug}/triggers"


def reconcile(now) -> list[Raise]:
    out: list[Raise] = []
    for trigger in (AgentSchedule.objects.filter(enabled=True)
                    .select_related("agent", "agent__owner", "created_by")):
        reason = triggers.refusal(trigger)
        if reason is None:
            continue
        agent = trigger.agent
        out.append(Raise(
            subject_key=f"trigger:{trigger.pk}",
            workspace=agent.workspace_id,
            title=f"{agent.name}'s trigger “{trigger.name}” has stopped firing",
            body=f"It fires with the authority of whoever set it up, and {reason}. "
                 "Restore their access, or re-create the trigger as yourself.",
            url=triggers_path(agent.workspace_id, agent.slug),
            scopes=(f"agent:{agent.slug}", f"workspace:{agent.workspace_id}"),
            fingerprint=hashlib.sha256(reason.encode()).hexdigest(),
        ))
    return out


def _trigger_of(alert):
    parts = alert.subject_key.split(":")
    if len(parts) != 2 or parts[0] != "trigger" or not parts[1].isdigit():
        return None
    return (AgentSchedule.objects.select_related("agent")
            .filter(pk=int(parts[1]), agent__workspace_id=alert.workspace_id).first())


def actions(alert, user) -> list[Action]:
    if not getattr(user, "is_authenticated", False):
        return []
    trigger = _trigger_of(alert)
    if trigger is None:
        return []
    if triggers.may_set_up(user, trigger.agent, kind=trigger.kind,
                           turn_mode=trigger.turn_mode) is not None:
        return []
    return [Action("triggers", "Open triggers", True,
                   href=triggers_path(trigger.agent.workspace_id, trigger.agent.slug))]


def perform(alert, user, action_id) -> None:
    # `triggers` is a link: nothing runs here.
    raise ValueError(f"trigger_stopped has no handler for {action_id!r}")

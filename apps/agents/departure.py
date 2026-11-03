"""What leaving a workspace takes with it, for agents: registered into `apps.workspaces.departure`."""
from __future__ import annotations

import logging

from apps.events import services as events
from apps.workspaces import departure

from .models import Agent, AgentAdmin, AgentCredential

log = logging.getLogger(__name__)


def _agent_admins(user, left, by) -> int:
    return AgentAdmin.objects.filter(user=user, agent__workspace_id__in=left).delete()[0]


def _delegations(user, left, by) -> int:
    # What they lent an agent (a GitHub token, their Google account) stops with them.
    return AgentCredential.objects.filter(delegated_by=user, agent__workspace_id__in=left).delete()[0]


def _agents_unowned(user, left, by) -> int:
    orphaned = list(Agent.objects.filter(owner=user, workspace_id__in=left))
    for agent in orphaned:
        agent.owner = None
        agent.save(update_fields=["owner", "updated_at"])
    if orphaned:
        _record_unowned(user, orphaned, by=by)
    return len(orphaned)


def _record_unowned(user, agents, *, by) -> None:
    """An agent losing its owner changes who it pushes and whose GitHub it uses,
    so it is said somewhere a workspace owner will look, not just done."""
    try:
        actor = getattr(by, "email", None) or "a workspace owner"
        for agent in agents:
            events.record([{
                "source": "workspaces.members", "kind": "agent.owner_cleared", "level": "warn",
                "summary": (f"{agent.slug} has no owner: {user.email} left the workspace "
                            f"(removed by {actor}). Transfer it from the agent's Settings."),
                "payload": {"agent": agent.slug, "former_owner": user.email,
                            "by": getattr(by, "email", None)},
            }], workspace=agent.workspace)
    except Exception:  # noqa: BLE001 — bookkeeping must never fail the removal
        log.exception("could not record an agent losing its owner")


def register() -> None:
    departure.register("agent_admins", _agent_admins, order=10)
    departure.register("delegations", _delegations, order=40)
    departure.register("agents_unowned", _agents_unowned, order=80)

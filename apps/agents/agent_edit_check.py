"""Preflight for gating the routes that change an agent on `agent_edit`: who loses edit rights.

Before the switch a person changes an agent if they hold `agent.work` in its workspace (an
editor or above) or are one of its admins. After it, only `access.may_edit_agent`. This lists
every (agent, person) that could edit before and cannot after, so the switch is merged only
once each one is accounted for. Read-only: it writes nothing.
"""
from __future__ import annotations

from apps.workspaces import permissions as perms
from apps.workspaces import services as wsvc

from . import access
from .models import Agent
from .standings import Standings


def edit_rights(agents=None) -> list[dict]:
    """One row per (agent, person) who may change the agent today: `before` is the old rule,
    `after` is `may_edit_agent`, `via` says what the new rule found (`decide`'s `source`)."""
    agents = list(agents if agents is not None else Agent.objects.select_related("workspace", "owner"))
    members = {ws: wsvc.effective_memberships(ws) for ws in {a.workspace_id for a in agents}}
    standings = Standings([m.user for ms in members.values() for m in ms], agents)
    rows = []
    for agent in agents:
        lines = standings.lines_of(agent)
        for m in members[agent.workspace_id]:
            st = standings.of(m.user, agent)
            before = perms.role_allows(m.role, perms.AGENT_WORK) or standings.is_admin(m.user, agent)
            if not before:
                continue
            d = access.decide(agent, m.user, verified=True, origin="canopy_web", standing=st, lines=lines)
            rows.append({
                "workspace": agent.workspace_id, "agent": agent.slug, "email": m.user.email,
                "workspace_role": m.role, "before": True,
                "after": access.may_edit_agent(m.user, agent, standing=st, lines=lines),
                "agent_role": d.agent_role, "via": d.source,
            })
    rows.sort(key=lambda r: (r["workspace"], r["agent"], r["email"]))
    return rows


def losses(agents=None) -> list[dict]:
    """The rows of `edit_rights` that lose the right."""
    return [r for r in edit_rights(agents) if r["before"] and not r["after"]]

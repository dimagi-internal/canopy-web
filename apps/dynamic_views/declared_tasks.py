"""The agent tasks a dynamic view may start: its `agent_tasks` declaration.

A view is read-only except for this one thing: its code may call
`canopy.runAgentTask({agent, name, ...})`, and the canopy page around the frame
starts that agent task (`POST /api/agent-tasks/`, docs/architecture/agent-tasks.md)
AS THE VIEWER — but only for an `{agent, name}` pair the view declares here.

The declaration is checked when the view is written, not on every draw: each
agent must be one of the view's workspace's agents, and the person saving it must
be someone who may dispatch to that agent themselves (the editor role in its
workspace, and THE rule, `access.decide`, not refusing them). So a view cannot
name an agent its author could not ask.

At run time the server's own gate on `POST /api/agent-tasks/` decides for the
viewer, whatever this declaration says; the page (`frontend/src/dynamicViews/
agentTasks.ts`) refuses anything undeclared before it asks.
"""
from __future__ import annotations

import re

from apps.agents import access
from apps.agents.models import Agent
from apps.workspaces import permissions as perms

#: A task name as `POST /api/agent-tasks/` takes it (1-120 characters), held to a
#: plain token so a declaration reads as what it is: `gmail-triage.review`.
NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,119}$")
MAX_DECLARED = 20


def may_dispatch(user, agent: Agent) -> str | None:
    """None when `user` may start a task for `agent` themselves, else why not.

    The same two gates `POST /api/agent-tasks/` runs for its caller: the editor
    role (`agent.work`) in the agent's workspace, then THE rule."""
    if not getattr(user, "is_authenticated", False):
        return "declaring an agent task needs a signed-in author"
    if not perms.can(user, agent.workspace_id, perms.AGENT_WORK):
        return (f"declaring a task for {agent.slug} needs the editor role in its workspace: "
                "the author must be able to give it a task themselves")
    d = access.decide(agent, user, verified=True, origin="api")
    if d.access == access.NONE:
        return d.reason or f"{agent.slug} does not take work from you"
    return None


def validate(workspace_id: str, declared, user) -> list[dict]:
    """Normalise a view's `agent_tasks`, or raise ValueError naming what is wrong."""
    if declared in (None, ""):
        return []
    if not isinstance(declared, list):
        raise ValueError("agent_tasks must be a list of {agent, name}")
    if len(declared) > MAX_DECLARED:
        raise ValueError(f"a view may declare at most {MAX_DECLARED} agent tasks")
    out: list[dict] = []
    seen: set[tuple[str, str]] = set()
    agents: dict[str, Agent] = {}
    for i, raw in enumerate(declared):
        if not isinstance(raw, dict):
            raise ValueError(f"agent_tasks[{i}] must be an object {{agent, name}}")
        extra = sorted(set(raw) - {"agent", "name"})
        if extra:
            raise ValueError(f"agent_tasks[{i}] takes only agent and name, not {', '.join(extra)}")
        slug = str(raw.get("agent") or "").strip()
        name = str(raw.get("name") or "").strip()
        if not slug:
            raise ValueError(f"agent_tasks[{i}].agent is required (an agent's slug)")
        if not NAME_RE.match(name):
            raise ValueError(f"agent_tasks[{i}].name must be 1-120 characters: letters, digits and _ . : -")
        if (slug, name) in seen:
            raise ValueError(f"agent_tasks[{i}] declares {slug} {name} twice")
        seen.add((slug, name))
        if slug not in agents:
            agent = Agent.objects.filter(workspace_id=workspace_id, slug=slug).first()
            if agent is None:
                raise ValueError(f"agent_tasks[{i}]: no agent '{slug}' in this view's workspace")
            if why := may_dispatch(user, agent):
                raise ValueError(f"agent_tasks[{i}]: {why}")
            agents[slug] = agent
        out.append({"agent": slug, "name": name})
    return out

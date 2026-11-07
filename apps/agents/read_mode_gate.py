"""The read-mode gate: which agents would be WIDENED by resolving `read` without a published read mode.

Spec 2026-11-03-agent-read-mode-and-doors-design.md, "Gate, at deploy time". Before read mode
resolved on its own, a `read_only` line (or `use:<READ_MODE_STAMP>` / `use:read`) on an agent that published no
read mode was inert. Now it lets that person in. This lists every such agent, with the lines, so a
person decides for each (keep, remove, or publish a read mode) before the change is deployed. Run it as

    manage.py read_mode_gate [--json]

and re-run it right before deploying and after. The result must be empty, or each hit has its
owner's decision.
"""
from __future__ import annotations

from . import grants, roles
from . import interface as iface_mod
from .models import Agent


def exposures() -> list[dict]:
    """[{agent, workspace, lines: [..]}] for every agent that has a line giving read mode (and not
    `write`: a writer already runs the whole agent), own or inherited from its workspaces, and has
    published no read-mode profile of its own."""
    agents = list(Agent.objects.select_related("workspace").order_by("workspace_id", "slug"))
    lines_of = grants.lines_for_many(agents)
    out = []
    for agent in agents:
        if iface_mod.read_mode_override(agent.interface) is not None:
            continue
        hits = [ln for ln in lines_of[agent.pk]
                if iface_mod.opens(ln.permissions, iface_mod.READ_MODE_NAME)
                and not roles.holds(ln.permissions, roles.WRITE)]
        if hits:
            out.append({"agent": agent.slug, "workspace": agent.workspace_id,
                        "lines": [f"{'workspace default' if ln.agent_id is None else 'agent'} "
                                  f"#{ln.pk}: {grants.describe(ln)} -> {ln.role or 'use ' + ln.capability}"
                                  for ln in hits]})
    return out

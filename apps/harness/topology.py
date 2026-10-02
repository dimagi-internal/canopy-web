"""The runner topology of a workspace subtree: which box serves which agent.

Routing is spread over four rows — `Workspace.parent` (the tree), `Agent.workspace`
(where an agent lives), `RunnerAssignment` (its ordered list + source/actor rules)
and `Runner` (where a box lives, who owns it, whether it is up). Each screen
showed one of them: the agent's Routing table one agent at a time, `/supervisor`
the boxes with no agents. "What does the connect division actually run on, and
what breaks if this laptop closes?" had no answer short of opening every agent.

Read-only, and computed — nothing here is stored, for the same reason turn status
is not a column: it is a function of rows that change on their own clock.

Who may read it: someone holding `logs.read` (admin+) on the root, and the
subtree is filtered to the workspaces where they hold it too (`visible`). An
owner of the root owns every descendant, so they see the whole tree; an admin
sees only where they are admin. Nothing here is beyond what that reader could
open agent by agent. A runner homed outside the subtree still appears when an agent
inside routes to it; its row carries only what the agent's own Routing table
already shows (name, kind, liveness) plus where it lives and who owns it, which
is the fact an owner needs to know who to call when it goes dark.
"""
from __future__ import annotations

from collections.abc import Callable

from apps.agents.models import Agent
from apps.workspaces import services as wsvc
from apps.workspaces.models import Workspace

from .models import Runner, RunnerAssignment
from .services import runner_tenant_slugs


def build(root: Workspace, visible: Callable[[str], bool] = lambda _slug: True) -> dict:
    """The topology rooted at `root`: its subtree (the descendants `visible`
    admits), every agent in it with its routing, and every runner those agents
    route to or that lives in the tree."""
    tree = wsvc.subtree(root, visible)
    slugs = {ws.slug for ws, _ in tree}

    agents = list(Agent.objects.filter(workspace_id__in=slugs).order_by("name"))
    assignments = list(
        RunnerAssignment.objects.filter(agent__in=agents)
        .select_related("runner", "runner__owner")
        .order_by("agent_id", "source", "actor", "rank")
    )
    home_runners = list(
        Runner.objects.filter(workspace_id__in=slugs)
        .exclude(status=Runner.RETIRED)
        .select_related("owner")
        .prefetch_related("declared_flags")
    )
    runners: dict = {r.pk: r for r in home_runners}
    for a in assignments:
        runners.setdefault(a.runner_id, a.runner)

    # One tenant lookup per runner, not per assignment: a runner may claim an
    # agent's turn only if its OWNER's workspaces include the agent's
    # (`services.runner_tenant_slugs`). An assignment that fails this is the
    # quietest misconfiguration there is — the row looks fine on the agent's
    # Routing table and its turns simply never get claimed.
    tenants = {pk: runner_tenant_slugs(r) for pk, r in runners.items()}

    routes_by_agent: dict[int, list[dict]] = {}
    serves: dict = {}
    for a in assignments:
        routes_by_agent.setdefault(a.agent_id, []).append({
            "runner_id": a.runner_id,
            "rank": a.rank,
            "enabled": a.enabled,
            "source": a.source,
            "actor": a.actor,
            "strict": a.strict,
            "turn_mode": a.turn_mode,
            "can_claim": None,  # filled below, once the agent's workspace is known
        })
        serves.setdefault(a.runner_id, set()).add(a.agent_id)

    agents_by_ws: dict[str, list[dict]] = {}
    for agent in agents:
        routes = routes_by_agent.get(agent.pk, [])
        for route in routes:
            route["can_claim"] = agent.workspace_id in tenants.get(route["runner_id"], set())
        agents_by_ws.setdefault(agent.workspace_id, []).append({
            "slug": agent.slug,
            "name": agent.name,
            "turn_mode": agent.turn_mode,
            "routes": routes,
        })

    ordered = [{
        "slug": ws.slug,
        "display_name": ws.display_name,
        "parent": ws.parent_id if depth else None,
        "depth": depth,
        "agents": agents_by_ws.get(ws.slug, []),
    } for ws, depth in tree]

    runner_rows = []
    for r in runners.values():
        runner_rows.append({
            "id": r.pk,
            "name": r.name,
            "kind": r.kind,
            "location": r.location,
            "status": r.live_status,
            "ready": r.ready,
            "ready_note": r.ready_note,
            "paused": r.paused,
            "host": r.host,
            "last_heartbeat_at": r.last_heartbeat_at,
            "workspace": r.workspace_id,
            "in_tree": r.workspace_id in slugs,
            "owner_email": r.owner.email if r.owner_id else None,
            "flags": sorted(f.flag for f in r.declared_flags.all()),
            "agent_count": len(serves.get(r.pk, ())),
        })
    runner_rows.sort(key=lambda row: (not row["in_tree"], row["name"].lower()))
    return {"root": root.slug, "workspaces": ordered, "runners": runner_rows}

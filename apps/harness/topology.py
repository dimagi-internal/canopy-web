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

from django.db.models import prefetch_related_objects

from apps.workspaces import services as wsvc
from apps.workspaces.models import Workspace, ancestor_chains

from .models import Runner, RunnerAssignment, WorkspaceRunnerOrder
from .routing import inherited_orders, runner_tenant_slugs_of


def code_current(runner) -> bool | None:
    """Whether the box runs the code the deployed server expects for its kind
    (`Runner.expected_code_sha`). None when either side is unknown — the
    supervisor's own rule: empty is unknown, never stale."""
    expected = runner.expected_code_sha()
    return (runner.code_sha == expected) if (runner.code_sha and expected) else None


def machine_label(runner) -> str:
    """The machine a runner is on, as the topology groups it (`Runner.machine_name`).
    Every runner on one machine shares it."""
    return runner.machine_name()


def unit_label(runner) -> str:
    """`tree:<root>` / `agent:<slug>` for a runner that declares what it serves
    (`Runner.unit`), "" otherwise."""
    unit = runner.unit()
    if unit.get("kind") == "agent":
        return f"agent:{unit.get('slug') or ''}"
    if unit.get("kind") == "tree":
        return f"tree:{unit.get('root') or ''}"
    return ""


def problems(runner) -> list[dict]:
    """Its self-reported health checks that are not ok, failures first."""
    checks = [c for c in ((runner.health or {}).get("checks") or [])
              if isinstance(c, dict) and c.get("status") in ("warn", "fail")]
    return [{"name": str(c.get("name", "")), "status": c["status"], "detail": str(c.get("detail", ""))[:500]}
            for c in sorted(checks, key=lambda c: c["status"] != "fail")]


def _usage(rows: list[dict], boxes: dict, viewer) -> None:
    """Each box's usage report on its row: when it last sent one, and whether the
    viewer administers it."""
    from .runners import administered_runner_ids

    administered = administered_runner_ids(viewer, boxes.values()) if viewer is not None else set()
    for row in rows:
        box = boxes.get(row["id"])
        row["usage_reported_at"] = box.usage_reported_at if box else None
        row["can_administer"] = row["id"] in administered


def build(root: Workspace, visible: Callable[[str], bool] = lambda _slug: True, viewer=None) -> dict:
    """The topology rooted at `root`: its subtree (the descendants `visible`
    admits), every agent in it with its routing, and every runner those agents
    route to or that lives in the tree — each runner as THE row both the
    Topology → Runners table and Health render."""
    from apps.agents.tree import agents_in_tree

    tree = wsvc.subtree(root, visible)
    slugs = {ws.slug for ws, _ in tree}

    agents = agents_in_tree(tree)
    assignments = list(
        RunnerAssignment.objects.filter(agent__in=agents)
        .select_related("runner", "runner__owner")
        .order_by("agent_id", "source", "actor", "rank")
    )
    home_runners = list(
        Runner.objects.filter(workspace_id__in=slugs)
        .exclude(status=Runner.RETIRED).exclude(kind=Runner.COMPUTER)
        .select_related("owner")
        .prefetch_related("declared_flags")
    )
    runners: dict = {r.pk: r for r in home_runners}
    for a in assignments:
        runners.setdefault(a.runner_id, a.runner)
    # Agents with no default order of their own follow their workspace's (or its
    # nearest ancestor's) — drawn as routes marked `inherited`.
    following = inherited_orders([a.pk for a in agents])
    for inh in following.values():
        for r in [*inh.runners, *inh.missing_repo, *inh.cannot_hold, *inh.directed]:
            runners.setdefault(r.pk, r)
    own_orders: dict[str, list] = {}
    for row in (WorkspaceRunnerOrder.objects.filter(workspace_id__in=slugs)
                .select_related("runner", "runner__owner").order_by("rank")):
        own_orders.setdefault(row.workspace_id, []).append(row)
        if row.runner.status != Runner.RETIRED:
            runners.setdefault(row.runner_id, row.runner)

    # One tenant lookup per runner, not per assignment: a runner may claim an
    # agent's turn only if its OWNER's workspaces include the agent's
    # (`services.runner_tenant_slugs`). An assignment that fails this is the
    # quietest misconfiguration there is — the row looks fine on the agent's
    # Routing table and its turns simply never get claimed.
    prefetch_related_objects(list(runners.values()), "enrollments")
    tenants = runner_tenant_slugs_of(runners.values())

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
        # A directed-only box listed on an agent never takes its routed work:
        # drawn (it is a row on the agent's list) but not counted as serving.
        if not a.runner.directed_only():
            serves.setdefault(a.runner_id, set()).add(a.agent_id)
    for agent_id, inh in following.items():
        for rank, r in enumerate(inh.runners):
            routes_by_agent.setdefault(agent_id, []).append({
                "runner_id": r.pk, "rank": rank, "enabled": True, "source": "", "actor": "",
                "strict": False, "turn_mode": "", "can_claim": None, "inherited": True,
            })
            serves.setdefault(r.pk, set()).add(agent_id)

    agents_by_ws: dict[str, list[dict]] = {}
    for agent in agents:
        routes = routes_by_agent.get(agent.pk, [])
        for route in routes:
            route["can_claim"] = (agent.workspace_id in tenants.get(route["runner_id"], set())
                                  and not runners[route["runner_id"]].directed_only())
        agents_by_ws.setdefault(agent.workspace_id, []).append({
            "slug": agent.slug,
            "name": agent.name,
            "turn_mode": agent.turn_mode,
            "routes": routes,
            "follows": ({
                "workspace": following[agent.pk].workspace,
                "missing_repo": [r.pk for r in following[agent.pk].missing_repo],
                "cannot_hold": [r.pk for r in following[agent.pk].cannot_hold],
                "directed": [r.pk for r in following[agent.pk].directed],
            } if agent.pk in following else None),
            "repo_url": agent.repo_url or "",
        })

    # Which workspaces (in the tree or above it) have an enabled order — one
    # query, rather than one per ancestor per workspace.
    ordered_anywhere = set(WorkspaceRunnerOrder.objects.filter(enabled=True)
                           .values_list("workspace_id", flat=True).distinct())
    chains = ancestor_chains({ws.slug: ws.parent_id for ws, _ in tree})
    ordered = [{
        "slug": ws.slug,
        "display_name": ws.display_name,
        "parent": ws.parent_id if depth else None,
        "depth": depth,
        "agents": agents_by_ws.get(ws.slug, []),
        "order": [{"runner_id": r.runner_id, "enabled": r.enabled} for r in own_orders.get(ws.slug, [])],
        "order_from": None if own_orders.get(ws.slug) else next(
            (s for s in chains[ws.slug] if s in ordered_anywhere), None),
    } for ws, depth in tree]

    by_pk = {a.pk: {"slug": a.slug, "name": a.name, "workspace": a.workspace_id} for a in agents}
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
            "status_note": r.status_note,
            "remedy": r.remedy,
            "paused": r.paused,
            "host": r.host,
            "last_heartbeat_at": r.last_heartbeat_at,
            "workspace": r.workspace_id,
            "in_tree": r.workspace_id in slugs,
            "owner_email": r.owner.email if r.owner_id else None,
            "flags": sorted(f.flag for f in r.declared_flags.all()),
            # Takes only what is put on it (`Runner.directed_only`): never routed work.
            "directed": r.directed_only(),
            "agent_count": len(serves.get(r.pk, ())),
            "code_current": code_current(r),
            "code_sha": r.code_sha,
            "expected_code_sha": r.expected_code_sha(),
            "agents": sorted((by_pk[a] for a in serves.get(r.pk, ()) if a in by_pk), key=lambda a: a["slug"]),
            "problems": problems(r),
            "machine": machine_label(r),
            "unit": unit_label(r),
        })
    # A machine's runners sit together, as one group: rows on one machine share a sort
    # key (the machine), and a runner on no known machine is a group of its own.
    runner_rows.sort(key=lambda row: (not row["in_tree"], (row["machine"] or row["name"]).lower(),
                                      row["name"].lower()))
    _usage(runner_rows, runners, viewer)
    return {"root": root.slug, "workspaces": ordered, "runners": runner_rows}

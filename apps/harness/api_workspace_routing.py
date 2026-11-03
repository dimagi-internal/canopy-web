"""A workspace's runner routing — the topology of runners and agents across its tree, and the
workspace's default runner order. Served under `/api/workspaces/{slug}/…` with the rest of
workspace settings; the logic is the harness's (`topology`, `routing`, `routing_audit`), so
the routes live here, above the workspace kit they use (`apps/workspaces/gates.py`)."""
from __future__ import annotations

from django.db import transaction
from django.http import HttpRequest
from ninja import Router
from ninja.errors import HttpError

from apps.api.auth import session_auth
from apps.api.route_gates import gate
from apps.workspaces import permissions as perms
from apps.workspaces.gates import require, tree_reader
from apps.workspaces.schemas import RunnerOrderIn, RunnerOrderRowOut, RunnerTopologyOut

from . import routing_audit, topology
from .models import Runner, WorkspaceRunnerOrder
from .routing import runner_tenant_slugs_of

router = Router(auth=session_auth, tags=["workspaces"])


@router.get("/{slug}/runner-topology", response=RunnerTopologyOut,
            summary="Which runners serve which agents, across this workspace and every one below it")
@gate("logs.read")  # root + each descendant where the caller holds it
def runner_topology(request: HttpRequest, slug: str) -> RunnerTopologyOut:
    """This workspace and its descendants, each with its agents and their routing
    (the default ordered list and every source rule), plus every runner those
    agents route to or that lives in the tree. Admin and above."""
    # `logs.read` — the operational read tier (runner drills, health) — on the
    # root, and again on every descendant: the tree carries owner and admin
    # down, so an admin of the root sees every division (`INHERITS_DOWN`).
    root, visible = tree_reader(request, slug)
    return RunnerTopologyOut(**topology.build(root, visible=visible, viewer=request.user))



@router.get("/{slug}/runner-order", response=list[RunnerOrderRowOut],
            summary="The workspace's default runner order: repo turns, and agents without their own")
@gate("member")
def get_runner_order(request: HttpRequest, slug: str) -> list[RunnerOrderRowOut]:
    """Repo turns (a project dispatch — no agent) route by this list: rank 0 takes
    the turn while it is available, the next rank only once every better one is
    not (or the turn has waited past the cascade grace), and a runner not listed
    never takes one. Empty: any runner that declares the repo.

    It is also the default order of every AGENT here that has none of its own,
    and of every workspace below this one that has no order of its own. Such an
    agent's source rules still come first. Member-readable, like an agent's own
    order."""
    m = require(request.user, slug, perms.READ)
    rows = (WorkspaceRunnerOrder.objects.filter(workspace=m.workspace)
            .select_related("runner").order_by("rank"))
    return [
        RunnerOrderRowOut(
            runner_id=row.runner_id, runner_name=row.runner.name, kind=row.runner.kind,
            rank=row.rank, online=row.runner.live_status == row.runner.ONLINE,
            ready=row.runner.ready, enabled=row.enabled,
            projects=row.runner.project_names(),
        )
        for row in rows
    ]


@router.put("/{slug}/runner-order", response=list[RunnerOrderRowOut],
            summary="Replace the workspace's default runner order")
# admin+; routes agents in every division below too; each runner's owner must be a member (422 otherwise)
@gate("runners.route", setting=True)
def set_runner_order(request: HttpRequest, slug: str, payload: RunnerOrderIn) -> list[RunnerOrderRowOut]:
    """Wholesale replace (index = rank). Workspace admins and owners
    (`runners.route`): the order routes agents in every workspace below this one
    too. Every runner must be able to SERVE this workspace — enrolled in it, its
    owner an editor or above (`runner_tenant_slugs`) — or the order would name a box the
    claim path refuses anyway; such a runner is a 422, as is an unknown or retired
    one."""
    # Maintainer note: was `agent.work` while the order routed only this
    # workspace's repo turns; an agent following it may live in a division where
    # an editor here holds no role at all. Then `own`; lowered to admin
    # (2026-10-05) — the runners it names must already serve this workspace.
    m = require(request.user, slug, perms.RUNNERS_ROUTE)
    ids = [row.runner_id for row in payload.runners]
    if len(ids) != len(set(ids)):
        raise HttpError(422, "duplicate runner id in list")
    runners = {r.id: r for r in Runner.objects.filter(id__in=ids)
               .exclude(status=Runner.RETIRED).exclude(kind=Runner.COMPUTER).select_related("owner")}
    missing = [str(rid) for rid in ids if rid not in runners]
    if missing:
        raise HttpError(422, f"unknown or retired runner id(s): {', '.join(missing)}")
    tenants = runner_tenant_slugs_of(runners.values())
    outside = sorted(r.name for r in runners.values() if slug not in tenants[r.pk])
    if outside:
        raise HttpError(422, f"runner(s) {', '.join(outside)} cannot serve '{slug}': "
                             "a runner must be enrolled in the workspace (POST "
                             "/api/harness/runners/{id}/workspaces) and its owner an "
                             "editor or above there")
    with transaction.atomic():
        before = routing_audit.workspace_rows(m.workspace)
        WorkspaceRunnerOrder.objects.filter(workspace=m.workspace).delete()
        WorkspaceRunnerOrder.objects.bulk_create([
            WorkspaceRunnerOrder(workspace=m.workspace, runner=runners[row.runner_id],
                                 rank=i, enabled=row.enabled)
            for i, row in enumerate(payload.runners)
        ])
        routing_audit.record_routing_change(
            request, workspace=m.workspace,
            target=f"workspace:{slug}", scope="default runner order",
            before=before, after=routing_audit.workspace_rows(m.workspace))
    return get_runner_order(request, slug)


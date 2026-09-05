"""Django Ninja router for the /api/agents surface — a first-class AI-agent
workspace (agents, their Google-Doc syncs, work products, and skill catalog)."""
from __future__ import annotations

from django.db import transaction
from django.http import HttpRequest
from ninja import Router, Status
from ninja.errors import HttpError

from apps.api.auth import session_auth
from apps.api.pagination import Page, clamp_limit, paginate
from apps.workspaces import services as wsvc

from . import services
from .schemas import (
    AgentCommandApplyIn,
    AgentDetailOut,
    AgentIn,
    AgentOut,
    AgentRunnerOut,
    AgentRunnerRuleOut,
    AgentRunnerRulesIn,
    AgentRunnerRowIn,
    AgentRunnersIn,
    AgentRuntimeOut,
    AgentSkillCatalogIn,
    AgentSkillOut,
    AgentSyncIn,
    AgentSyncOut,
    AgentTaskCommandIn,
    AgentTaskCommandOut,
    AgentTaskIn,
    AgentTaskOut,
    AgentTaskPatch,
    AgentTaskSyncIn,
    AgentTurnIn,
    AgentTurnOut,
    AgentWorkProductBatchIn,
    AgentWorkProductOut,
    CommandResultOut,
    CountOut,
    RunnerPreferenceIn,
    TurnModeIn,
)

router = Router(auth=session_auth, tags=["agents"])


def _visible_agent_workspace_ids(request: HttpRequest) -> set[str]:
    """The single definition of 'agent workspaces this caller can see'. A
    workspace_id is visible if the caller is pinned to it, or — unpinned —
    the caller is a member of it. _get_agent_or_404, list_agents, and the
    fleet items query MUST build from this: they used to hand-copy this
    predicate three times, which is exactly the failure apps/harness/api.py's
    _runner_visibility_q docstring describes (a runner the list showed but
    every action 404'd on) — see that docstring for the full story.

    Tenant-pinned (request.workspace_slug truthy): exactly that workspace —
    no separate membership check needed; WorkspaceResolveMiddleware already
    gated membership of the pinned workspace before setting workspace_slug.

    Not pinned (flat /api/agents/... callers): any workspace the caller is a
    member of.

    Fails CLOSED on an unhomed agent (security review 2026-07-26, hole A):
    this used to return the caller's workspace ids **plus {None}**, so an
    agent with no workspace was visible to ANY authenticated user across the
    whole agents surface — reads (tasks, work products, skills, turns,
    including AgentTurnOut.share_token, a public transcript link) AND writes
    (board commands, PUT /runners). Strictly broader than the read-only hole
    `_agent_or_404` (apps/harness/api.py, F1) closed for the same
    workspace-less-agent case. An unhomed agent must be unresolvable via this
    API, not universally visible. Since agents/0013 made `Agent.workspace` NOT
    NULL the case is unrepresentable rather than merely unpopulated, so this
    reads as a plain membership check with nothing to special-case."""
    wsvc.auto_join_workspaces(request.user)
    ws = getattr(request, "workspace_slug", None)
    if ws:
        return {ws}
    return set(wsvc.user_workspace_slugs(request.user))


def _get_agent_or_404(request: HttpRequest, slug: str):
    """Resolve an agent, gated by workspace membership. A non-member gets the
    same 404 as a missing agent (no existence leak). Domain users are auto-joined
    to the agent's workspace first, so the default-workspace case keeps working."""
    agent = services.get_agent(slug)
    if agent is None:
        raise HttpError(404, f"agent '{slug}' not found")
    if agent.workspace_id not in _visible_agent_workspace_ids(request):
        raise HttpError(404, f"agent '{slug}' not found")  # wrong tenant / non-member
    return agent


@router.get("/", response=Page[AgentOut], summary="List agents",
            openapi_extra={"x-mcp-expose": True})
def list_agents(request: HttpRequest, limit: int = 100) -> Page[AgentOut]:
    limit = clamp_limit(limit)
    visible = _visible_agent_workspace_ids(request)
    items = [
        AgentOut.model_validate(a)
        for a in services.list_agents()
        if a.workspace_id in visible
    ]
    return paginate(items, offset=0, limit=limit)


@router.post("/", response={201: AgentOut}, summary="Create or update an agent (upsert by slug)",
             openapi_extra={"x-mcp-expose": True})
def upsert_agent(request: HttpRequest, payload: AgentIn) -> Status:
    # The tenant is resolved BEFORE the row is written, because Agent.workspace is
    # NOT NULL (agents/0013) — an agent is never briefly unhomed. Scope to the
    # request's workspace (from the /w/{ws} prefix or the compat shim's default),
    # falling back to the org default so an unchanged register() (e.g. Echo's)
    # keeps working.
    wsvc.auto_join_workspaces(request.user)
    pinned = getattr(request, "workspace_slug", None)
    home = (
        wsvc.Workspace.objects.filter(slug=pinned).first() if pinned else None
    ) or wsvc.ensure_default_workspace()
    if home is None:
        # Only reachable on a DB with no users at all, which an authenticated
        # request cannot be. Fail with a real message rather than an IntegrityError.
        raise HttpError(422, "no workspace available to home this agent in")
    agent = services.upsert_agent(payload, workspace=home)
    explicit = (payload.workspace or "").strip()
    if explicit and agent.workspace_id != explicit:
        # Explicit home: may MOVE an already-homed agent. Membership-gated; a
        # missing workspace and a non-member get the same 404 (no existence leak).
        ws = wsvc.Workspace.objects.filter(slug=explicit).first()
        if ws is None or not wsvc.is_member(request.user, explicit):
            raise HttpError(404, f"workspace '{explicit}' not found")
        agent.workspace = ws
        agent.save(update_fields=["workspace"])
    wsvc.ensure_member(agent.workspace, request.user)  # creator keeps access
    return Status(201, AgentOut.model_validate(agent))


@router.get("/{slug}/", response=AgentDetailOut, summary="Agent detail (with counts)",
            openapi_extra={"x-mcp-expose": True})
def get_agent(request: HttpRequest, slug: str) -> AgentDetailOut:
    agent = _get_agent_or_404(request, slug)
    return AgentDetailOut.model_validate(services.agent_detail(agent))


@router.delete("/{slug}/", response={204: None}, summary="Delete an agent (editor/owner)",
               openapi_extra={"x-mcp-expose": True})
def delete_agent(request: HttpRequest, slug: str):
    """Remove an agent and everything hanging off it.

    Registration (`POST /`) was a one-way door until this existed: an agent
    created by mistake — a typo'd slug, a scaffold someone was only trying
    out, a test — was permanent and fleet-visible to every member of its
    workspace, because the only way back was a shell on the box. That made
    *rehearsing* the onboarding path impossible: you could not walk a new
    operator's steps end to end without leaving a fake agent behind forever.

    Gated one step ABOVE creation deliberately. Any member may upsert an
    agent; deleting one requires editor or owner, so a viewer cannot destroy
    a fleet member's board. `_get_agent_or_404` runs first, so a non-member
    gets 404 (no existence leak) rather than 403.

    Every FK into Agent is CASCADE or SET_NULL (runs, turns, tasks, skills,
    syncs, work products, schedules, items, runner assignments/drills), so
    this is a real delete rather than a soft flag — nothing is left dangling
    and nothing blocks it.
    """
    agent = _get_agent_or_404(request, slug)
    membership = wsvc.WorkspaceMembership.objects.filter(
        user=request.user, workspace_id=agent.workspace_id
    ).first()
    allowed = {wsvc.WorkspaceMembership.OWNER, wsvc.WorkspaceMembership.EDITOR}
    if membership is None or membership.role not in allowed:
        raise HttpError(403, "deleting an agent requires the editor or owner role")
    agent.delete()
    return Status(204, None)


@router.patch("/{slug}/runner-preference", response=AgentDetailOut,
              summary="Set an agent's ordered runner-kind preference")
def set_runner_preference(request: HttpRequest, slug: str, payload: RunnerPreferenceIn) -> AgentDetailOut:
    """DEPRECATED: superseded by PUT /api/agents/{slug}/runners; removed next release.

    Update just the ordered runner-kind preference (cloud/emdash/remote), no
    clobber of the agent's other fields. Honored at claim time — see
    harness.services.claim_next_turn."""
    from apps.harness.models import Runner

    valid = {k for k, _ in Runner.KIND_CHOICES}
    bad = [k for k in payload.runner_preference if k not in valid]
    if bad:
        raise HttpError(422, f"unknown runner kind(s): {', '.join(bad)}")
    agent = _get_agent_or_404(request, slug)
    agent.runner_preference = list(payload.runner_preference)
    agent.save(update_fields=["runner_preference", "updated_at"])
    return AgentDetailOut.model_validate(services.agent_detail(agent))


@router.patch("/{slug}/turn-mode", response=AgentDetailOut,
              summary="Set an agent's turn mode (manual | auto)")
def set_turn_mode(request: HttpRequest, slug: str, payload: TurnModeIn) -> AgentDetailOut:
    """Flip the agent's runtime autonomy posture — the board-side switch the
    fleet turn procedure reads at preflight (agent-core/turn.md § Turn mode).
    A human decision made from the board; the agent-repo upsert (POST /) cannot
    touch this field."""
    agent = _get_agent_or_404(request, slug)
    agent.turn_mode = payload.turn_mode
    agent.save(update_fields=["turn_mode", "updated_at"])
    return AgentDetailOut.model_validate(services.agent_detail(agent))


@router.get("/{slug}/runtime", response=AgentRuntimeOut,
            summary="Agent runtime info — how a runner provisions + runs this agent")
def get_agent_runtime(request: HttpRequest, slug: str) -> AgentRuntimeOut:
    """The registry entry point (Agent Runtime Registry). A runner (PAT-authed)
    asks 'how do I run agent X?' and gets the repo pointer, the secret-reference
    names to resolve, the engine preference, and the tenant. Tenant-gated exactly
    like every other agent read."""
    agent = _get_agent_or_404(request, slug)
    return AgentRuntimeOut(
        slug=agent.slug,
        repo_url=agent.repo_url,
        repo_ref=agent.repo_ref,
        engine=agent.runtime_engine,
        secret_refs=list(agent.runtime_secrets or []),
        workspace=agent.workspace_id,
    )


# ---- runner assignments (the routing-matrix UI's read/write surface) ----
@router.get("/{slug}/runners", response=list[AgentRunnerOut],
            summary="List the agent's ordered runner assignments")
def list_agent_runners(request: HttpRequest, slug: str) -> list[AgentRunnerOut]:
    agent = _get_agent_or_404(request, slug)
    return [
        AgentRunnerOut(
            runner_id=a.runner_id,
            runner_name=a.runner.name,
            kind=a.runner.kind,
            rank=a.rank,
            online=a.runner.live_status == a.runner.ONLINE,
            ready=a.runner.ready,
            enabled=a.enabled,
        )
        # source="" ONLY: this endpoint is the DEFAULT ordered list. A source
        # rule lives in the same table and would otherwise render as a phantom
        # chip in the order row.
        for a in agent.runner_assignments.filter(source="").select_related("runner")
    ]


@router.put("/{slug}/runners", response=list[AgentRunnerOut],
            summary="Replace the agent's ordered runner list (index = rank)")
def replace_agent_runners(request: HttpRequest, slug: str, payload: AgentRunnersIn) -> list[AgentRunnerOut]:
    """Replace the agent's ORDERED runner list (index = rank) — the single
    routing authority (spec 2026-07-24). Wholesale replace: the matrix UI saves
    a full row, so there is no partial-update ambiguity.

    Accepts either form (exactly one must be provided — 422 otherwise):
    `runners` (ordered rows, each carrying its own `enabled` — a disabled row
    stays in the list, rank preserved, but never routes) or the legacy
    `runner_ids` (ordered ids, all implicitly enabled)."""
    from apps.harness.api import _runner_visibility_q
    from apps.harness.models import Runner, RunnerAssignment

    agent = _get_agent_or_404(request, slug)

    if (payload.runner_ids is None) == (payload.runners is None):
        raise HttpError(422, "provide exactly one of runner_ids or runners")

    if payload.runners is not None:
        rows_in: list[AgentRunnerRowIn] = payload.runners
    else:
        rows_in = [AgentRunnerRowIn(runner_id=rid, enabled=True) for rid in payload.runner_ids]

    ids = [row.runner_id for row in rows_in]
    # Reject duplicate runner IDs early
    if len(ids) != len(set(ids)):
        raise HttpError(422, "duplicate runner id in list")
    # Scoped by the same _runner_visibility_q predicate apps/harness/api.py's
    # _runner_or_404/list_runners gate on — a runner_id the caller can't see
    # (paired by someone else, wrong tenant) must 422 as "unknown", never be
    # attachable/readable just because its UUID was guessed. See that
    # docstring for the full predicate story.
    runners = list(
        Runner.objects.filter(id__in=ids)
        .exclude(status=Runner.RETIRED)
        .filter(_runner_visibility_q(request))
    )
    by_id = {r.id: r for r in runners}
    missing = [str(rid) for rid in ids if rid not in by_id]
    if missing:
        raise HttpError(422, f"unknown or retired runner id(s): {', '.join(missing)}")
    with transaction.atomic():
        # source="" ONLY. Source rules live in this table too, and an unscoped
        # delete here would destroy every one of them each time the default
        # order was saved.
        RunnerAssignment.objects.filter(agent=agent, source="").delete()
        RunnerAssignment.objects.bulk_create([
            RunnerAssignment(agent=agent, runner=by_id[row.runner_id], rank=i, enabled=row.enabled)
            for i, row in enumerate(rows_in)
        ])
    return list_agent_runners(request, slug)


# ---- per-source routing rules (the exceptions to the ordered list above) ----
@router.get("/{slug}/runner-rules", response=list[AgentRunnerRuleOut],
            summary="List the agent's per-source routing rules")
def list_agent_runner_rules(request: HttpRequest, slug: str) -> list[AgentRunnerRuleOut]:
    """The per-source overrides on top of the default ordered list (spec
    2026-07-27). One rule per source, max — the priority runner, and whether it is
    the only one allowed to take that source's work."""
    from collections import Counter

    from apps.harness.actors import actor_of
    from apps.harness.models import Runner, RunnerAssignment, Turn

    agent = _get_agent_or_404(request, slug)
    # Queued depth per (source, actor) — what the UI's "N turns are parked" warning
    # renders next to a strict rule whose runner is offline. This cannot stay a
    # `values_list("origin").annotate(Count)` now that the key includes the actor:
    # the actor is DERIVED per turn (from `origin_ref["from"]` for mail, from
    # `enqueued_by` otherwise), so it is grouped in Python. Queued sets are single
    # digits in practice, and a count keyed only on origin would attribute other
    # people's parked work to your rule.
    queued: Counter = Counter()
    for t in (
        Turn.objects.filter(agent=agent, status=Turn.QUEUED)
        .select_related("enqueued_by")
        .only("origin", "origin_ref", "enqueued_by")
    ):
        queued[(t.origin, actor_of(t))] += 1

    rows = (
        RunnerAssignment.objects.filter(agent=agent).exclude(source="")
        .select_related("runner").order_by("source", "actor", "rank")
    )
    return [
        AgentRunnerRuleOut(
            source=row.source,
            actor=row.actor,
            rank=row.rank,
            runner_id=row.runner.id,
            runner_name=row.runner.name,
            kind=row.runner.kind,
            strict=row.strict,
            # Derived, like AgentRunnerOut: the stored status lies once a runner
            # goes quiet (heartbeat writes ONLINE and nothing demotes it).
            online=row.runner.live_status == Runner.ONLINE,
            ready=row.runner.ready,
            enabled=row.enabled,
            # Every row of a rule repeats its RULE's count: the parked queue belongs
            # to the rule, not to one runner in it.
            queued_count=queued.get((row.source, row.actor), 0),
        )
        for row in rows
    ]


@router.put("/{slug}/runner-rules", response=list[AgentRunnerRuleOut],
            summary="Replace the agent's per-source routing rules")
def replace_agent_runner_rules(
    request: HttpRequest, slug: str, payload: AgentRunnerRulesIn
) -> list[AgentRunnerRuleOut]:
    """Wholesale replace, scoped to non-empty-source rows — the default ordered
    list belongs to PUT /runners and is left alone.

    A separate endpoint rather than a combined body so neither write can clobber
    the other's rows (they share one table), and so the existing GET response
    shape stays what the frontend already consumes.
    """
    from apps.harness.actors import normalize_actor
    from apps.harness.api import _runner_visibility_q
    from apps.harness.models import Runner, RunnerAssignment

    agent = _get_agent_or_404(request, slug)

    # Normalize BEFORE validating or storing, so a rule pasted straight out of a
    # mail client ("Sarvesh Tewari <STewari@Dimagi.com>") matches a turn whose
    # sender resolves to the bare lowercase address. Rejecting an unparseable
    # actor is the point: stored as-is it would be a rule that silently never
    # matches, which is indistinguishable from a routing bug.
    actors: list[str] = []
    for r in payload.rules:
        actor = normalize_actor(r.actor) if r.actor else ""
        if r.actor and not actor:
            raise HttpError(422, f"not an email address: {r.actor!r}")
        actors.append(actor)

    # One rule per (source, actor) — several actors MAY share a source, which is
    # the whole feature. Caught here rather than left to the DB constraint so the
    # caller gets a named reason instead of an IntegrityError 500.
    keys = list(zip([r.source for r in payload.rules], actors))
    if len(keys) != len(set(keys)):
        raise HttpError(422, "one rule per (source, actor): duplicate in list")

    for r, actor in zip(payload.rules, actors):
        if not r.runners:
            # A zero-length strict rule composes to an empty list and parks the
            # queue naming no runner as the reason. Deleting the rule is how you
            # turn it off.
            raise HttpError(422, f"a rule needs at least one runner: {r.source}/{actor}")
        seen = [row.runner_id for row in r.runners]
        if len(seen) != len(set(seen)):
            raise HttpError(422, f"a runner may appear once per rule: {r.source}/{actor}")

    # Same visibility predicate the default-list PUT gates on — a runner the
    # caller can't see must 422 as unknown, never be attachable by guessed UUID.
    ids = [row.runner_id for r in payload.rules for row in r.runners]
    runners = list(
        Runner.objects.filter(id__in=ids)
        .exclude(status=Runner.RETIRED)
        .filter(_runner_visibility_q(request))
    )
    by_id = {r.id: r for r in runners}
    missing = [str(rid) for rid in ids if rid not in by_id]
    if missing:
        raise HttpError(422, f"unknown or retired runner id(s): {', '.join(missing)}")

    with transaction.atomic():
        RunnerAssignment.objects.filter(agent=agent).exclude(source="").delete()
        RunnerAssignment.objects.bulk_create([
            # `rank` is the runner's position WITHIN its rule — the list order the
            # caller sent. `strict` is rule-level and written to every row of the
            # rule, so the composer can read it off the first.
            RunnerAssignment(
                agent=agent, runner=by_id[row.runner_id], rank=rank,
                source=r.source, actor=actor, strict=r.strict, enabled=row.enabled,
            )
            for r, actor in zip(payload.rules, actors)
            for rank, row in enumerate(r.runners)
        ])
    return list_agent_runner_rules(request, slug)


# ---- syncs (Google-Doc backed) ----
@router.get("/{slug}/syncs/", response=Page[AgentSyncOut], summary="List the agent's syncs",
            openapi_extra={"x-mcp-expose": True})
def list_syncs(request: HttpRequest, slug: str, limit: int = 100) -> Page[AgentSyncOut]:
    limit = clamp_limit(limit)
    agent = _get_agent_or_404(request, slug)
    items = [AgentSyncOut.model_validate(s) for s in services.list_syncs(agent, limit=limit)]
    return paginate(items, offset=0, limit=limit)


@router.post("/{slug}/syncs/", response={201: AgentSyncOut},
             summary="Post a Google-Doc sync (idempotent per period+source)",
             openapi_extra={"x-mcp-expose": True})
def create_sync(request: HttpRequest, slug: str, payload: AgentSyncIn) -> Status:
    agent = _get_agent_or_404(request, slug)
    sync = services.upsert_sync(agent, payload)
    return Status(201, AgentSyncOut.model_validate(sync))


@router.delete("/{slug}/syncs/{sync_id}/", response={204: None},
               summary="Delete a sync (wrong period / stray record)",
               openapi_extra={"x-mcp-expose": True})
def delete_sync(request: HttpRequest, slug: str, sync_id: int) -> Status:
    """POST upserts per (period, source), so re-posting only corrects a sync for the
    SAME window — a sync filed under the wrong period is otherwise unreachable."""
    agent = _get_agent_or_404(request, slug)
    if not services.delete_sync(agent, sync_id):
        raise HttpError(404, f"sync {sync_id} not found for agent '{slug}'")
    return Status(204, None)


# ---- turns (a packaged unit of work + optional transcript link) ----
@router.get("/{slug}/turns/", response=Page[AgentTurnOut], summary="List the agent's turns",
            openapi_extra={"x-mcp-expose": True})
def list_turns(request: HttpRequest, slug: str, limit: int = 100) -> Page[AgentTurnOut]:
    limit = clamp_limit(limit)
    agent = _get_agent_or_404(request, slug)
    items = [AgentTurnOut.model_validate(t) for t in services.list_turns(agent, limit=limit)]
    return paginate(items, offset=0, limit=limit)


@router.post("/{slug}/turns/", response={201: AgentTurnOut},
             summary="Package a turn (idempotent per cli_session_id)",
             openapi_extra={"x-mcp-expose": True})
def create_turn(request: HttpRequest, slug: str, payload: AgentTurnIn) -> Status:
    agent = _get_agent_or_404(request, slug)
    turn = services.upsert_turn(agent, payload)
    return Status(201, AgentTurnOut.model_validate(turn))


# ---- work products ----
@router.get("/{slug}/work-products/", response=Page[AgentWorkProductOut],
            summary="List the agent's work products",
            openapi_extra={"x-mcp-expose": True})
def list_work_products(request: HttpRequest, slug: str, limit: int = 200) -> Page[AgentWorkProductOut]:
    limit = clamp_limit(limit)
    agent = _get_agent_or_404(request, slug)
    items = [AgentWorkProductOut.model_validate(w) for w in services.list_work_products(agent, limit=limit)]
    return paginate(items, offset=0, limit=limit)


@router.post("/{slug}/work-products/", response=CountOut,
             summary="Add/update work products (upsert by url)",
             openapi_extra={"x-mcp-expose": True})
def add_work_products(request: HttpRequest, slug: str, payload: AgentWorkProductBatchIn) -> CountOut:
    agent = _get_agent_or_404(request, slug)
    result = services.upsert_work_products(agent, payload.work_products)
    return CountOut(**result)


# ---- skill catalog ----
@router.get("/{slug}/skills/", response=list[AgentSkillOut], summary="List the agent's skill catalog",
            openapi_extra={"x-mcp-expose": True})
def list_skills(request: HttpRequest, slug: str) -> list[AgentSkillOut]:
    agent = _get_agent_or_404(request, slug)
    return [AgentSkillOut.model_validate(s) for s in services.list_skills(agent)]


@router.put("/{slug}/skills/", response=CountOut, summary="Replace the agent's skill catalog",
            openapi_extra={"x-mcp-expose": True})
def replace_skills(request: HttpRequest, slug: str, payload: AgentSkillCatalogIn) -> CountOut:
    agent = _get_agent_or_404(request, slug)
    count = services.replace_skills(agent, payload.skills)
    return CountOut(count=count)


# ---- tasks (board) ----
@router.get("/{slug}/tasks/", response=list[AgentTaskOut], summary="List the agent's tasks (board)",
            openapi_extra={"x-mcp-expose": True})
def list_tasks(request: HttpRequest, slug: str) -> list[AgentTaskOut]:
    agent = _get_agent_or_404(request, slug)
    return [AgentTaskOut.model_validate(t) for t in services.list_tasks(agent)]


@router.post("/{slug}/tasks/sync", response=CountOut,
             summary="Upsert the agent's tasks from the (legacy) source sheet",
             openapi_extra={"x-mcp-expose": True})
def sync_tasks(request: HttpRequest, slug: str, payload: AgentTaskSyncIn) -> CountOut:
    agent = _get_agent_or_404(request, slug)
    return CountOut(**services.sync_tasks(agent, payload.tasks))


def _get_task_or_404(agent, task_id: int):
    task = services.get_task(agent, task_id)
    if task is None:
        raise HttpError(404, f"task {task_id} not found")
    return task


@router.post("/{slug}/tasks/", response={201: AgentTaskOut}, summary="Create a task",
             openapi_extra={"x-mcp-expose": True})
def create_task(request: HttpRequest, slug: str, payload: AgentTaskIn) -> Status:
    agent = _get_agent_or_404(request, slug)
    return Status(201, AgentTaskOut.model_validate(services.create_task(agent, payload)))


@router.patch("/{slug}/tasks/{task_id}/", response=AgentTaskOut, summary="Update a task",
              openapi_extra={"x-mcp-expose": True})
def patch_task(request: HttpRequest, slug: str, task_id: int, payload: AgentTaskPatch) -> AgentTaskOut:
    agent = _get_agent_or_404(request, slug)
    task = _get_task_or_404(agent, task_id)
    data = payload.model_dump(exclude_unset=True)
    return AgentTaskOut.model_validate(services.patch_task(task, data))


# ---- task commands (the board's action queue) ----
@router.post("/{slug}/tasks/{task_id}/commands", response={201: CommandResultOut},
             summary="Post a board action (accept/decline/dispatch/…) on a task",
             openapi_extra={"x-mcp-expose": True})
def post_command(request: HttpRequest, slug: str, task_id: int, payload: AgentTaskCommandIn) -> Status:
    agent = _get_agent_or_404(request, slug)
    task = _get_task_or_404(agent, task_id)
    created_by = payload.created_by or getattr(request.user, "email", "")
    cmd = services.create_command(agent, task, payload.kind, payload.payload, created_by)
    return Status(201, CommandResultOut(
        command=AgentTaskCommandOut.model_validate(cmd),
        task=AgentTaskOut.model_validate(cmd.task) if cmd.task_id else None,
    ))


@router.get("/{slug}/commands", response=list[AgentTaskCommandOut],
            summary="List commands (the agent reads ?status=pending)",
            openapi_extra={"x-mcp-expose": True})
def list_commands(request: HttpRequest, slug: str, status: str | None = None) -> list[AgentTaskCommandOut]:
    agent = _get_agent_or_404(request, slug)
    return [AgentTaskCommandOut.model_validate(c) for c in services.list_commands(agent, status)]


@router.post("/{slug}/commands/{cmd_id}/apply", response=AgentTaskCommandOut,
             summary="Mark a command applied (the agent calls this after acting)",
             openapi_extra={"x-mcp-expose": True})
def apply_command(request: HttpRequest, slug: str, cmd_id: int, payload: AgentCommandApplyIn) -> AgentTaskCommandOut:
    agent = _get_agent_or_404(request, slug)
    cmd = agent.commands.filter(id=cmd_id).select_related("task", "agent").first()
    if cmd is None:
        raise HttpError(404, f"command {cmd_id} not found")
    return AgentTaskCommandOut.model_validate(services.apply_command(cmd, payload.result_note))

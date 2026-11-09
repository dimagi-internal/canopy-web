"""/api/harness runner routes: flags, private, directed, name, only-agents, egress and engine.
"""
from __future__ import annotations

import uuid

from canopy_sdk import contract
from django.db.models import Q
from django.http import HttpRequest
from ninja.errors import HttpError

from apps.agents.models import Agent
from apps.api.route_gates import gate
from apps.events import audit as events_audit
from apps.workspaces import services as wsvc

from . import (
    egress,
    routing,
)
from . import runners as harness_runners
from .api import (  # noqa: E402
    router,
    runner_admin_or_404,
    runner_or_404,
)
from .api_runners import _with_roots
from .models import (
    PrivateRunner,
    Runner,
)
from .runner_requirements import is_sensitive_data_box
from .schemas import (
    RunnerDirectedIn,
    RunnerEgressIn,
    RunnerEngineIn,
    RunnerFlagsIn,
    RunnerNameIn,
    RunnerOnlyAgentsIn,
    RunnerOnlyAgentsOut,
    RunnerOut,
    RunnerPrivateIn,
)


@router.put("/runners/{runner_id}/flags", response=RunnerOut,
            summary="Declare what this runner's owner vouches for")
@gate("runner-admin", setting=True, step_up=True)
def set_runner_flags(request: HttpRequest, runner_id: uuid.UUID, payload: RunnerFlagsIn):
    """Replace the runner's declared flags. `sensitive_data`: this box is set up for
    sensitive data (a ZDR API key for Claude, a locked harness, egress limited to an
    allowlist, nothing kept after a turn). canopy cannot check a declaration; it
    records who made it. A host may require a flag of every conversation its
    visitors hold, and those conversations then run only on runners declaring it.
    """
    runner = runner_admin_or_404(request, runner_id)
    # A person vouching for a box, never a token: not a PAT, not an MCP call, and
    # not the box's own RunnerAccessToken (which runner_auth lets reach every route
    # under its own runner). 403 `step_up_required` -> the SPA asks for the passkey
    # and retries; a token gets a plain 403 it cannot answer.
    from apps.passkeys import stepup

    stepup.require_recent_step_up(request)
    try:
        wanted = set(contract.parse_runner_requirements(payload.flags))
    except ValueError as exc:
        raise HttpError(422, str(exc))
    added, removed = harness_runners.set_runner_flags(runner, wanted, by=request.user)
    if runner.workspace_id and (added or removed):
        from apps.events import services as events

        events.record([
            {"source": "harness.runners", "kind": kind, "level": "info",
             "summary": f"{request.user.email} {verb} {flag} on {runner.name}",
             "payload": {"runner": str(runner.pk), "flag": flag, "by": request.user.email}}
            for kind, verb, names in (("runner.flag_declared", "declared", added),
                                      ("runner.flag_withdrawn", "withdrew", removed))
            for flag in sorted(names)
        ], workspace=runner.workspace)
    out = Runner.objects.prefetch_related("declared_flags").get(pk=runner.pk)
    # Per (caller, runner), as list_runners stamps them: RunnerOut defaults both
    # to True, so an unstamped reply would show an admin who is not the owner
    # controls that then 404.
    out.can_manage = out.owner_id in (request.user.id, None)
    out.can_administer = harness_runners.can_administer_runner(request.user, out)
    return _with_roots(request, out)


@router.put("/runners/{runner_id}/private", response=RunnerOut,
            summary="Make this runner its owner's private runner")
@gate("runner", setting=True)  # owner only (not RunnerAdmins)
def set_runner_private(request: HttpRequest, runner_id: uuid.UUID, payload: RunnerPrivateIn):
    """A private runner takes no fleet work: nothing routed, no workspace order, no
    source rule — only what its owner pins or binds to it, and the direct sessions
    opened on it. Owner only. (To run as a lower role, start the TURN with
    `acts_as` — it is a property of one turn, not of a machine.)"""
    # Owner only, not runner admins: it decides whose work the box
    # takes, which is the owner's to say.
    from apps.events import audit

    runner = runner_or_404(request, runner_id)
    if runner.owner_id != request.user.pk:
        raise HttpError(404, "only the runner's owner can make it private")
    before = {"private": runner.private}
    after = {"private": payload.private}
    if before != after:
        runner.private = payload.private
        runner.save(update_fields=["private"])
    if runner.private:
        # The box IS its owner's private runner: link the registration here, so the
        # runner writes it on its next heartbeat and no one runs `canopy private-runner
        # register` on the machine. Its OWN registration — never another machine's
        # that shares its name (`local_turns.private_runner_for_runner`).
        from . import local_turns

        local_turns.private_runner_for_runner(runner)
    else:
        # Shared again: the registration stops naming a box whose token now speaks
        # for the fleet. Making it private again links (or makes) one afresh.
        PrivateRunner.objects.filter(runner=runner).update(runner=None)
    audit.note_change(scope=f"runner-private:{runner.name}", before=before, after=after,
                      workspace=runner.workspace)
    out = Runner.objects.prefetch_related("declared_flags").get(pk=runner.pk)
    out.can_manage = True
    out.can_administer = harness_runners.can_administer_runner(request.user, out)
    return _with_roots(request, out)


@router.put("/runners/{runner_id}/directed", response=RunnerOut,
            summary="Decide whether this runner takes only work put on it")
@gate("runner-admin", setting=True)
def set_runner_directed(request: HttpRequest, runner_id: uuid.UUID, payload: RunnerDirectedIn):
    """`true`: the box takes only turns pinned to it and chats bound to it, no routed
    work. `false`: it takes the work routed to it. Owner or a runner administrator;
    a private runner stays directed either way."""
    runner = runner_admin_or_404(request, runner_id)
    before = {"directed": runner.directed}
    if runner.directed != payload.directed:
        runner.directed = payload.directed
        runner.save(update_fields=["directed"])
    events_audit.note_change(scope=f"runner-directed:{runner.name}", before=before,
                             after={"directed": runner.directed}, workspace=runner.workspace)
    out = (Runner.objects.select_related("owner", "paused_by", "private_runner")
           .prefetch_related("declared_flags").get(pk=runner.pk))
    out.can_manage = out.owner_id in (request.user.id, None)
    out.can_administer = harness_runners.can_administer_runner(request.user, out)
    return _with_roots(request, out)


@router.put("/runners/{runner_id}/name", response=RunnerOut,
            summary="Rename this runner")
@gate("runner-admin", setting=True)
def set_runner_name(request: HttpRequest, runner_id: uuid.UUID, payload: RunnerNameIn):
    """The name people see for this runner everywhere in canopy. Owner or a runner
    administrator. Routing, pins and the vault follow the runner, not its name, so
    nothing else changes. The box keeps the name it paired as (`paired_as`)."""
    # Pairing matches on `paired_as` as well as `name` (api_runner_invitations
    # pair_by_role, machine_pairing._machine_runner), so a replaced instance still
    # re-attaches to a renamed runner.
    runner = runner_admin_or_404(request, runner_id)
    name = payload.name.strip()
    if not name:
        raise HttpError(422, "a runner's name cannot be blank")
    before = {"name": runner.name}
    if runner.name != name:
        runner.name = name
        runner.save(update_fields=["name"])
    events_audit.note_change(scope=f"runner-name:{runner.pk}", before=before,
                             after={"name": runner.name}, workspace=runner.workspace)
    out = (Runner.objects.select_related("owner", "paused_by", "private_runner")
           .prefetch_related("declared_flags").get(pk=runner.pk))
    out.can_manage = out.owner_id in (request.user.id, None)
    out.can_administer = harness_runners.can_administer_runner(request.user, out)
    return _with_roots(request, out)


@router.get("/runners/{runner_id}/only-agents", response=RunnerOnlyAgentsOut,
            summary="The agents this runner is dedicated to, and the ones it could be")
@gate("runner-admin")
def get_runner_only_agents(request: HttpRequest, runner_id: uuid.UUID):
    """The current list, and every agent it could hold: in the workspaces the box
    serves and visible to you. Owner or a runner administrator."""
    runner = runner_admin_or_404(request, runner_id)
    current = runner.only_agent_slugs()
    # An agent already on the list stays a choice even if you could not see it,
    # so unticking it is always possible.
    choices = (Agent.objects.filter(workspace_id__in=routing.runner_tenant_slugs(runner))
               .filter(Q(workspace_id__in=wsvc.user_workspace_slugs(request.user))
                       | Q(slug__in=current))
               .order_by("slug"))
    return {"agents": sorted(current),
            "choices": [{"slug": a.slug, "name": a.name, "workspace": a.workspace_id}
                        for a in choices]}


@router.put("/runners/{runner_id}/only-agents", response=RunnerOut,
            summary="Dedicate this runner to a list of agents")
@gate("runner-admin", setting=True)
def set_runner_only_agents(request: HttpRequest, runner_id: uuid.UUID, payload: RunnerOnlyAgentsIn):
    """Replace the agents this runner is dedicated to. Listed: it runs only their
    turns and chats — no other agent's, no repo turn, no agentless chat — however
    they reach it, a pin included. [] lifts the restriction. It never ADDS work:
    an agent still routes here only through its runner list or the order it
    follows. Each agent must be one the box may serve (in its tenant). Owner or a
    runner administrator."""
    runner = runner_admin_or_404(request, runner_id)
    wanted = {s.strip() for s in payload.agents if s.strip()}
    found = {a.slug: a for a in Agent.objects.filter(
        slug__in=wanted, workspace_id__in=routing.runner_tenant_slugs(runner))}
    missing = sorted(wanted - set(found))
    if missing:
        raise HttpError(422, f"{runner.name} cannot serve {', '.join(missing)}: no such agent "
                             "in the workspaces it serves")
    before = sorted(routing.dedicated_agent_slugs(runner) or ())
    after = sorted(found)
    if before != after:
        runner.only_agents.set(found.values())
    events_audit.note_change(scope=f"runner-only-agents:{runner.name}", before=before, after=after,
                      workspace=runner.workspace)
    out = (Runner.objects.select_related("owner", "paused_by", "private_runner")
           .prefetch_related("declared_flags").get(pk=runner.pk))
    out.can_manage = out.owner_id in (request.user.id, None)
    out.can_administer = harness_runners.can_administer_runner(request.user, out)
    return _with_roots(request, out)


@router.put("/runners/{runner_id}/egress", response=RunnerOut,
            summary="Lock this runner's egress to an allowlist of hosts")
@gate("runner-admin", setting=True, step_up=True)
def set_runner_egress(request: HttpRequest, runner_id: uuid.UUID, payload: RunnerEgressIn):
    """Set the runner's egress lock: `locked` (its runners reach the network only
    through the box's proxy, to canopy-web, the model and GitHub) and `allow`, the
    hosts it admits beyond those (`.example.com` for every subdomain). Root on the box
    applies it within a minute, on top of the box's own setting, which it never
    loosens. A sensitive-data box is always locked; adding a host to one needs a person in a
    browser behind a passkey. Owner or a runner administrator."""
    runner = runner_admin_or_404(request, runner_id)
    try:
        allow = egress.normalize_allow(payload.allow)
    except ValueError as exc:
        raise HttpError(422, str(exc))
    forced = is_sensitive_data_box(runner)
    if forced and not payload.locked:
        raise HttpError(422, egress.FORCED)
    if forced and egress.loosens(runner, locked=payload.locked, allow=allow):
        # A sensitive-data box reaching one more host is a person's decision, as its
        # `sensitive_data` declaration is: 403 `step_up_required` for a browser, a plain 403 for a token.
        from apps.passkeys import stepup  # noqa: PLC0415 — lazy like set_runner_flags' own

        stepup.require_recent_step_up(request)
    before = {"locked": runner.egress_locked, "allow": list(runner.egress_allow or [])}
    after = {"locked": payload.locked, "allow": allow}
    if before != after:
        runner.egress_locked, runner.egress_allow = payload.locked, allow
        runner.save(update_fields=["egress_locked", "egress_allow"])
    events_audit.note_change(scope=f"runner-egress:{runner.name}", before=before, after=after,
                             workspace=runner.workspace)
    out = (Runner.objects.select_related("owner", "paused_by", "private_runner")
           .prefetch_related("declared_flags").get(pk=runner.pk))
    out.can_manage = out.owner_id in (request.user.id, None)
    out.can_administer = harness_runners.can_administer_runner(request.user, out)
    return _with_roots(request, out)


@router.put("/runners/{runner_id}/engine", response=RunnerOut,
            summary="Choose the session runtime a laptop runner opens new sessions in")
@gate("runner-admin", setting=True)
def set_runner_engine(request: HttpRequest, runner_id: uuid.UUID, payload: RunnerEngineIn):
    """Set the runtime (`emdash`, `claude-desktop` or `claude-cli`) a laptop runner
    opens new sessions in. Takes effect on the runner's next heartbeat; sessions already
    running stay where they are."""
    # The runner reads `engine` off its heartbeat response, so a
    # flip needs no restart and no shell on the box, and is safe while sessions are
    # live: a thread already running in one runtime keeps its own session, and
    # routing is the same either way. Owner or runner admins, like flags — it
    # changes how the box works, not whose memberships it speaks with. A cloud
    # runner has one runtime (Claude headless) and refuses rather than ignoring it.
    runner = runner_admin_or_404(request, runner_id)
    if payload.engine not in dict(Runner.ENGINE_CHOICES):
        raise HttpError(422, f"unknown runtime '{payload.engine}' — one of: "
                             + ", ".join(dict(Runner.ENGINE_CHOICES)))
    if runner.kind == Runner.CLOUD:
        raise HttpError(422, "a cloud runner runs Claude headless; it has no session runtime to choose")
    if runner.engine != payload.engine:
        before = runner.engine
        runner.engine = payload.engine
        runner.save(update_fields=["engine"])
        if runner.workspace_id:
            from apps.events import services as events

            events.record([{
                "source": "harness.runners", "kind": "runner.engine_changed", "level": "info",
                "summary": f"{request.user.email} switched {runner.name} from {before} to {payload.engine}",
                "payload": {"runner": str(runner.pk), "from": before, "to": payload.engine,
                            "by": request.user.email},
            }], workspace=runner.workspace)
    out = Runner.objects.prefetch_related("declared_flags").get(pk=runner.pk)
    out.can_manage = out.owner_id in (request.user.id, None)
    out.can_administer = harness_runners.can_administer_runner(request.user, out)
    return _with_roots(request, out)

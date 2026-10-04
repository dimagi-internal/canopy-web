"""Django Ninja router for the /api/agents surface — a first-class AI-agent
workspace (agents, their Google-Doc syncs, work products, and skill catalog)."""
from __future__ import annotations

import datetime as dt
import logging
from typing import Any

from django.db import transaction
from django.http import HttpRequest
from ninja import Router, Status
from ninja.errors import HttpError

from apps.api.auth import session_auth
from apps.api.pagination import Page, clamp_limit, paginate
from apps.workspaces import permissions as perms
from apps.workspaces import services as wsvc

from . import delegations, services
from . import skill_history as history
from .models import AgentTaskCommand
from .schemas import (
    AgentInterfaceIn,
    AgentInterfaceOut,
    AgentAccessOut,
    AgentAdminOut,
    AgentCommandApplyIn,
    AgentCredentialsIn,
    AgentGitHubIn,
    AgentCanopyUserIn,
    AgentGitHubOut,
    AgentCredentialsResolveOut,
    AgentCredentialStatusOut,
    AgentDetailOut,
    AgentIn,
    AgentOut,
    AgentOwnerIn,
    AgentActorRouteIn,
    AgentActorRouteOut,
    AgentActorRouteRunnerOut,
    AgentDefaultOrderOut,
    AgentRunnerOut,
    AgentRunnerRowIn,
    AgentRunnerRuleOut,
    AgentRunnerRulesIn,
    AgentRunnersIn,
    AgentRuntimeOut,
    AgentSkillCatalogIn,
    AgentSkillOut,
    AgentSyncIn,
    AgentSyncOut,
    AgentTaskCommandIn,
    AgentTaskCommandOut,
    AgentTaskIn,
    AgentProjectIn,
    AgentProjectOut,
    AgentProjectPatch,
    AgentTaskOut,
    AgentTaskPatch,
    AgentTaskSyncIn,
    AgentTurnIn,
    AgentTurnOut,
    AgentVaultIn,
    AgentVaultOut,
    AgentWorkProductBatchIn,
    AgentWorkProductOut,
    BootstrapReportIn,
    BootstrapReportOut,
    CommandResultOut,
    CountOut,
    RunnerPreferenceIn,
    SkillHistoryOut,
    SlackEnabledIn,
    SlackEnabledOut,
    TurnModeIn,
)

logger = logging.getLogger(__name__)

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
    ws = getattr(request, "workspace_slug", None)
    if ws:
        return {ws}
    return set(wsvc.user_workspace_slugs(request.user))


def _get_agent_or_404(request: HttpRequest, slug: str):
    """Resolve an agent, gated by workspace membership. A non-member gets the
    same 404 as a missing agent (no existence leak). A domain user who has not
    been let into the agent's workspace (an invite, or an approved access
    request) is a non-member and gets exactly that 404 — nothing joins
    automatically."""
    agent = services.get_agent(slug)
    if agent is None:
        raise HttpError(404, f"agent '{slug}' not found")
    if agent.workspace_id not in _visible_agent_workspace_ids(request):
        raise HttpError(404, f"agent '{slug}' not found")  # wrong tenant / non-member
    return agent


def _can(request: HttpRequest, workspace, capability: str) -> bool:
    """May the caller exercise `capability` in `workspace`? The request-shaped
    door to `apps/workspaces/permissions.py`, the one table of what each role
    may do. This module names capabilities, never roles."""
    return perms.can(request.user, workspace, capability)


def _agent_for_write(request: HttpRequest, slug: str):
    """An agent the caller may RESHAPE — create, edit, schedule, route, publish.

    The author/executor tier. Separate from `_get_agent_or_404` (membership,
    the interaction tier) because a `viewer` may talk to an agent and read its
    board without being able to change what the agent IS.

    `_get_agent_or_404` runs FIRST, so a non-member still gets `404` and never
    a `403` — no existence leak. Follows the ordering already established at
    `delete_agent` (below): resolve-then-authorize, never the other way.
    """
    agent = _get_agent_or_404(request, slug)
    if not _can(request, agent.workspace_id, perms.AGENT_WORK):
        raise HttpError(403, "this action requires the editor role or above")
    return agent


def _refuse_runners_that_cannot_hold(agent, runners) -> None:
    """403 naming every runner whose owner is not one of the agent's admins.

    Pointing an agent's work at a box hands that box the agent's whole identity
    (`services.runner_may_hold_agent`), and these writes are open to the editor
    tier — so the box, not just the caller, has to be trusted with the agent.
    """
    refused = sorted({r.name for r in runners if not services.runner_may_hold_agent(r, agent)})
    if refused:
        raise HttpError(
            403,
            f"runner(s) {', '.join(refused)} cannot run {agent.slug}: a runner's owner "
            "must be the agent's owner, a workspace owner, or one of its admins",
        )


def _refuse_auto_unless_admin(request: HttpRequest, agent, what: str) -> None:
    """`auto` lets the agent act outbound without review, so SETTING it — on a
    routing rule, an actor route or the agent's own switch — is for the agent's
    owner and admins (docs/architecture/access.md). A workspace editor may still
    route the agent's work and set `manual`; lowering autonomy is safe from
    anyone. Before 2026-10-04 an editor could write `turn_mode: auto` on a rule,
    which ran every matching turn auto — around the dispatch gate (#1102) that
    keeps `auto` from them."""
    if not agent.is_admin(request.user):
        raise HttpError(
            403,
            f"{what} with turn_mode=auto is for {agent.slug}'s owner or admins; you may set "
            "it with turn_mode manual or '' (defer), or ask its owner to make you an admin",
        )


def _agent_for_admin(request: HttpRequest, slug: str):
    """An agent whose SECRETS the caller may change: its owner or an admin.

    Credentials and the vault pointer are the keys a runner resolves
    everything else from, so writing them is equivalent to controlling the
    agent end to end. Decided in the who-is-asking spec (D7): the agent's own
    owner or any of its admins — an explicit, per-agent grant (`AgentAdmin`).
    This used to mean "WORKSPACE owner" alone, because self-join hands out
    `editor` and owner was the only role that meant something had been
    granted. Workspace owners remain admins (`Agent.is_admin`), so nobody who
    held this gate lost it; the agent's owner and explicit admins gained it.

    `_get_agent_or_404` runs FIRST, same ordering as `_agent_for_write`: a
    non-member gets `404`, never `403`.
    """
    agent = _get_agent_or_404(request, slug)
    if not agent.is_admin(request.user):
        raise HttpError(403, "this action requires the agent's owner or an admin")
    return agent


@router.get("/", response=Page[AgentOut], summary="List agents",)
def list_agents(request: HttpRequest, limit: int = 100) -> Page[AgentOut]:
    limit = clamp_limit(limit)
    visible = _visible_agent_workspace_ids(request)
    items = [
        AgentOut.model_validate(a)
        for a in services.list_agents()
        if a.workspace_id in visible
    ]
    return paginate(items, offset=0, limit=limit)


@router.post("/", response={201: AgentOut}, summary="Create or update an agent (upsert by slug)",)
def upsert_agent(request: HttpRequest, payload: AgentIn) -> Status:
    # The tenant is resolved BEFORE the row is written, because Agent.workspace is
    # NOT NULL (agents/0013) — an agent is never briefly unhomed. A home matters
    # only on CREATE (`services.upsert_agent` applies it via create_defaults), so
    # an EXISTING agent keeps the one it has: Echo re-registers flat on every
    # sync, and that must neither move it nor depend on what its caller's
    # default workspace happens to be.
    #
    # A new agent homes in the explicit `workspace` when given (the caller must
    # be a member — same 404 as below), else in `wsvc.creation_workspace`: the
    # membership-bound resolver every other create uses. It used to fall back to
    # `ensure_default_workspace()`, which guessed the org default for any flat
    # caller whether or not they were in it; now an unpinned create lands in a
    # workspace the caller is actually in, or 422s when that is ambiguous.
    existing = services.get_agent(payload.slug)
    explicit = (payload.workspace or "").strip()
    if existing is not None:
        home = existing.workspace
    elif explicit:
        home = wsvc.Workspace.objects.filter(slug=explicit).first()
        if home is None or not wsvc.is_member(request.user, explicit):
            raise HttpError(404, f"workspace '{explicit}' not found")
    else:
        home = wsvc.creation_workspace(request)
        if home is None:
            raise HttpError(
                422,
                "no unambiguous workspace to home this agent in; "
                "post to /api/w/{workspace}/agents/ or pass `workspace`",
            )

    # This is the reshaping tier (same as _agent_for_write), but there is no
    # existing agent to resolve through _get_agent_or_404 on a create — so the
    # gate is against the TARGET workspace directly: an already-existing
    # agent's CURRENT home (this write reshapes that tenant's row, whatever
    # workspace the caller happens to default into), or `home` for a
    # brand-new agent — which is the same thing, `home` above.
    role = wsvc.member_role(request.user, home)
    if existing is not None and role is None:
        # Resolve-then-authorize, the ordering `_agent_for_write` gets for free
        # from `_get_agent_or_404`. It has to be spelled out here because this
        # route takes the slug in the BODY, so there may be no agent to resolve
        # at all.
        #
        # `Agent.slug` is globally UNIQUE, so answering 403 to a non-member of
        # the existing agent's workspace made this route an oracle over the
        # entire slug namespace: 201 means the slug is free, 403 means an agent
        # by that name exists in a tenant you cannot see. Every editor of any
        # workspace could enumerate the fleet's names. A non-member now gets
        # the same 404 `GET /{slug}/` would give them — no existence leak —
        # while a member holding only `viewer` still gets 403 below, which
        # tells them something true about their own tenant.
        #
        # Scoped to `existing is not None` deliberately: on a genuine CREATE
        # there is nothing to leak, and 404-ing a create would be a lie about
        # the only fact the caller already knows.
        raise HttpError(404, f"agent '{payload.slug}' not found")
    if not perms.role_allows(role, perms.AGENT_WORK):
        raise HttpError(403, "creating or editing an agent requires the editor role or above")
    # A MOVE also requires admin of the agent where it is — checked before
    # anything is written. Moving re-decides every gate that hangs off its
    # tenant (a workspace owner is an agent admin), so an editor (the self-join
    # default) could otherwise move an agent into a workspace they had just
    # created, own it there, and hold its keys.
    if (existing is not None and explicit and existing.workspace_id != explicit
            and not existing.is_admin(request.user)):
        raise HttpError(403, "moving an agent requires being its owner or one of its admins")

    agent = services.upsert_agent(payload, workspace=home)
    if explicit and agent.workspace_id != explicit:
        # Explicit home: may MOVE an already-homed agent. A missing workspace
        # and a non-member get the same 404 (no existence leak); moving also
        # requires editor/owner in the DESTINATION — moving a tenant's agent
        # is a reshape like everything else this tier gates.
        ws = wsvc.Workspace.objects.filter(slug=explicit).first()
        if ws is None or not wsvc.is_member(request.user, explicit):
            raise HttpError(404, f"workspace '{explicit}' not found")
        if not _can(request, ws, perms.AGENT_WORK):
            raise HttpError(403, "moving an agent requires the editor role or above in the destination workspace")
        agent.workspace = ws
        agent.save(update_fields=["workspace"])
    # No `ensure_member` here any more, and it is not an omission: since the
    # role gate above, every path that reaches this line has already required
    # editor-or-owner in `agent.workspace` — on a create, on an edit, and in the
    # destination of a move. So the call could only ever be a no-op, while
    # reading as though registration were a way to join a tenant. That shape is
    # exactly what `wsvc.creation_workspace`'s docstring is about.
    return Status(201, AgentOut.model_validate(agent))


def _may_transfer_owner(request: HttpRequest, agent) -> bool:
    """A workspace owner, or the agent's current owner."""
    if agent.owner_id is not None and agent.owner_id == request.user.pk:
        return True
    return _can(request, agent.workspace_id, perms.OWN)


def _may_manage_admins(request: HttpRequest, agent) -> bool:
    """Granting admin hands over the agent's credentials (D7), so it is held
    to the same bar as transferring the agent: its owner or a workspace owner."""
    return _may_transfer_owner(request, agent)


def _detail(request: HttpRequest, agent) -> AgentDetailOut:
    return AgentDetailOut.model_validate({
        **services.agent_detail(agent),
        "can_transfer_owner": _may_transfer_owner(request, agent),
        "is_admin": agent.is_admin(request.user),
        "can_manage_admins": _may_manage_admins(request, agent),
    })


@router.get("/{slug}/", response=AgentDetailOut, summary="Agent detail (with counts)",)
def get_agent(request: HttpRequest, slug: str) -> AgentDetailOut:
    agent = _get_agent_or_404(request, slug)
    return _detail(request, agent)


# Browser-only, like owner and admin changes: linking a canopy user makes it
# count as the agent itself (never confined against itself — agents/access.py),
# so it is a decision a PERSON makes in the canopy UI. The owner or an admin.
@router.put("/{slug}/canopy-user", response=AgentDetailOut,
            summary="Link this agent to the canopy user it is")
def link_canopy_user(request: HttpRequest, slug: str, payload: AgentCanopyUserIn) -> AgentDetailOut:
    """The canopy user account this agent's own token signs in as. `user_id`
    null unlinks it."""
    agent = _get_agent_or_404(request, slug)
    if not agent.is_admin(request.user):
        raise HttpError(403, "only the agent's owner or an admin can change its canopy user")
    try:
        services.link_canopy_user(agent, payload.user_id)
    except services.AgentUserLinkError as exc:
        raise HttpError(422, str(exc)) from exc
    return _detail(request, agent)


# The owner is whose GitHub grant the agent's GitHub-backed features read
# through, so moving it is held to the agent's owner or a workspace owner. A
# token (PAT, MCP client) acts as its user and may do it too: anything the web
# app can do, MCP can (2026-10-02 — browser-only gates were tried and removed).
# Resolve first so a non-member still gets 404, never 403.
@router.put("/{slug}/owner", response=AgentDetailOut,
            summary="Transfer the agent's ownership to a member of its workspace")
def transfer_owner(request: HttpRequest, slug: str, payload: AgentOwnerIn) -> AgentDetailOut:
    agent = _get_agent_or_404(request, slug)
    if not _may_transfer_owner(request, agent):
        raise HttpError(403, "only a workspace owner or the agent's current owner can transfer it")
    if payload.user_id is None:
        if not _can(request, agent.workspace_id, perms.OWN):
            raise HttpError(403, "only a workspace owner can leave an agent without an owner")
        agent.owner = None
    else:
        from django.contrib.auth import get_user_model

        target = get_user_model().objects.filter(pk=payload.user_id).first()
        # A question about the TARGET, asked through the one authorizer.
        if target is None or not wsvc.is_member(target, agent.workspace_id):
            raise HttpError(422, "the new owner must be a member of this agent's workspace")
        agent.owner = target
    agent.save(update_fields=["owner", "updated_at"])
    return _detail(request, agent)


def _admin_rows(agent) -> list[dict]:
    from .models import AgentAdmin

    rows = []
    # Workspace owners are admins implicitly (`Agent.is_admin`); they are not
    # listed here, which is "who was granted this agent", and the members page
    # already answers "who owns the workspace".
    if agent.owner is not None:
        rows.append({"user_id": agent.owner.pk, "email": agent.owner.email,
                     "name": agent.owner.get_full_name() or agent.owner.email,
                     "is_owner": True})
    for g in (AgentAdmin.objects.filter(agent=agent).exclude(user_id=agent.owner_id)
              .select_related("user", "granted_by")):
        # An inert grant (holder left the workspace) is not listed: it grants
        # nothing, and showing it would say otherwise.
        if not wsvc.is_member(g.user, agent.workspace_id):  # authz-exempt: a question about the TARGET
            continue
        rows.append({"user_id": g.user.pk, "email": g.user.email,
                     "name": g.user.get_full_name() or g.user.email,
                     "granted_by_email": g.granted_by.email if g.granted_by else None,
                     "granted_at": g.granted_at})
    return rows


def _interface_out(agent) -> dict:
    by = agent.interface_published_by
    return {"interface": agent.interface or {}, "source": agent.interface_source or "",
            "published_at": agent.interface_published_at,
            "published_by_email": by.email if by is not None else None}


@router.get("/{slug}/interface", response=AgentInterfaceOut,
            summary="What this agent offers callers — its declared interface")
def get_interface(request: HttpRequest, slug: str):
    return _interface_out(_get_agent_or_404(request, slug))


# Owner-or-admin, not the editor tier the skill catalog uses: the interface is
# a SECURITY POLICY — it decides what people outside the agent's admins can make
# it do — so loosening it is the same kind of act as handing over its keys.
@router.put("/{slug}/interface", response=AgentInterfaceOut,
            summary="Save the agent's declared interface (YAML source, or a parsed mapping)")
def publish_interface(request: HttpRequest, slug: str, payload: AgentInterfaceIn):
    import yaml
    from django.utils import timezone

    from .interface import InterfaceError, parse

    agent = _agent_for_admin(request, slug)
    if (payload.source is None) == (payload.interface is None):
        raise HttpError(422, "send exactly one of `source` (YAML) or `interface` (a mapping)")
    doc, source = payload.interface, ""
    if payload.source is not None:
        source = payload.source
        try:
            # safe_load: no tags, no object construction — it is a person's text.
            doc = yaml.safe_load(source) or {}
        except yaml.YAMLError as exc:
            raise HttpError(422, f"not valid YAML: {exc}") from exc
    try:
        agent.interface = parse(doc)
    except InterfaceError as exc:
        raise HttpError(422, str(exc)) from exc
    agent.interface_source = source
    agent.interface_published_at = timezone.now()
    agent.interface_published_by = request.user
    agent.save(update_fields=["interface", "interface_source", "interface_published_at",
                              "interface_published_by", "updated_at"])
    return _interface_out(agent)


@router.delete("/{slug}/interface", response=AgentInterfaceOut,
               summary="Unpublish the declared interface: every turn runs in the full profile again")
def unpublish_interface(request: HttpRequest, slug: str):
    agent = _agent_for_admin(request, slug)
    agent.interface = {}
    agent.interface_source = ""
    agent.interface_published_at = None
    agent.interface_published_by = None
    agent.save(update_fields=["interface", "interface_source", "interface_published_at",
                              "interface_published_by", "updated_at"])
    return _interface_out(agent)


@router.get("/{slug}/admins", response=list[AgentAdminOut],
            summary="Who holds this agent's keys: its owner and admins")
def list_admins(request: HttpRequest, slug: str):
    return _admin_rows(_get_agent_or_404(request, slug))


@router.get("/{slug}/access", response=AgentAccessOut,
            summary="Everyone's role on this agent, why, and what they can reach")
def agent_access(request: HttpRequest, slug: str):
    """Every member of the agent's workspace with their role on the agent
    (owner / admin / member), the reason for it, and what they reach signed in:
    the whole agent, the capabilities its published interface lists for them,
    or nothing. `outsiders` lists the interface rules that reach people outside
    the workspace. Readable by any member, like the admin list."""
    from . import access

    agent = _get_agent_or_404(request, slug)
    iface = agent.interface or {}
    return {
        "members": access.roster(agent),
        "outsiders": access.outsiders(agent),
        "interface_published": bool(iface.get("capabilities") or iface.get("full")),
        "slack_enabled": agent.slack_enabled,
    }


# Granting admin hands over the agent's credentials, so it is held to the same
# bar as transferring the agent: its owner or a workspace owner (by browser or
# MCP alike). Resolve first so a non-member gets 404.
def _admin_change_gate(request: HttpRequest, slug: str):
    agent = _get_agent_or_404(request, slug)
    if not _may_manage_admins(request, agent):
        raise HttpError(403, "only the agent's owner or a workspace owner can change its admins")
    return agent


@router.put("/{slug}/admins/{user_id}", response=list[AgentAdminOut],
            summary="Make a workspace member an admin of this agent")
def grant_admin(request: HttpRequest, slug: str, user_id: int):
    from django.contrib.auth import get_user_model

    from .models import AgentAdmin

    agent = _admin_change_gate(request, slug)
    target = get_user_model().objects.filter(pk=user_id).first()
    # A question about the TARGET, asked through the one authorizer.
    if target is None or not wsvc.is_member(target, agent.workspace_id):
        raise HttpError(422, "an admin must be a member of this agent's workspace")
    AgentAdmin.objects.get_or_create(agent=agent, user=target,
                                     defaults={"granted_by": request.user})
    return _admin_rows(agent)


@router.delete("/{slug}/admins/{user_id}", response=list[AgentAdminOut],
               summary="Revoke an admin of this agent")
def revoke_admin(request: HttpRequest, slug: str, user_id: int):
    from .models import AgentAdmin

    agent = _admin_change_gate(request, slug)
    if agent.owner_id == user_id:
        raise HttpError(422, "the owner is always an admin; transfer ownership instead")
    AgentAdmin.objects.filter(agent=agent, user_id=user_id).delete()
    return _admin_rows(agent)


@router.delete("/{slug}/", response={204: None}, summary="Delete an agent (editor/owner)",)
def delete_agent(request: HttpRequest, slug: str):
    """Remove an agent and everything hanging off it.

    Registration (`POST /`) was a one-way door until this existed: an agent
    created by mistake — a typo'd slug, a scaffold someone was only trying
    out, a test — was permanent and fleet-visible to every member of its
    workspace, because the only way back was a shell on the box. That made
    *rehearsing* the onboarding path impossible: you could not walk a new
    operator's steps end to end without leaving a fake agent behind forever.

    Gated at the same reshaping tier as everything else `_agent_for_write`
    covers (Phase 0 of the agent-instances-and-ACL design closed the old gap
    where deletion was the ONLY gated write on this surface — everything else,
    including the credential/vault writers, was membership-or-nothing): a
    viewer cannot destroy a fleet member's board. `_agent_for_write` resolves
    the agent via `_get_agent_or_404` first, so a non-member gets 404 (no
    existence leak) rather than 403.

    Every FK into Agent is CASCADE or SET_NULL (runs, turns, tasks, skills,
    syncs, work products, schedules, items, runner assignments/drills), so
    this is a real delete rather than a soft flag — nothing is left dangling
    and nothing blocks it.
    """
    agent = _agent_for_write(request, slug)
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
    agent = _agent_for_write(request, slug)
    agent.runner_preference = list(payload.runner_preference)
    agent.save(update_fields=["runner_preference", "updated_at"])
    return _detail(request, agent)


@router.patch("/{slug}/turn-mode", response=AgentDetailOut,
              summary="Set an agent's turn mode (manual | auto)")
def set_turn_mode(request: HttpRequest, slug: str, payload: TurnModeIn) -> AgentDetailOut:
    """Flip the agent's runtime autonomy posture — the board-side switch the
    fleet turn procedure reads at preflight (agent-core/turn.md § Turn mode).
    A human decision made from the board; the agent-repo upsert (POST /) cannot
    touch this field. `manual` from any editor; `auto` from the agent's owner or
    an admin — it is the last rung of the routing ladder ("Everything else")."""
    agent = _agent_for_write(request, slug)
    if payload.turn_mode == "auto" and agent.turn_mode != "auto":
        _refuse_auto_unless_admin(request, agent, "setting the agent's turn mode")
    agent.turn_mode = payload.turn_mode
    agent.save(update_fields=["turn_mode", "updated_at"])
    return _detail(request, agent)


@router.patch("/{slug}/slack", response=SlackEnabledOut,
              summary="Turn Slack access to an agent on or off")
def set_slack_enabled(request: HttpRequest, slug: str, payload: SlackEnabledIn) -> SlackEnabledOut:
    """Whether people in this workspace's connected Slack can talk to the agent
    (by mention, DM, `/canopy <slug>` or `/<slug>`). Owner only. Adds or removes
    the agent's `/<slug>` command in the Slack app when canopy manages it."""
    # Owner, not editor: editor is what anyone who self-joined already holds,
    # and this opens the agent to everyone in a Slack workspace.
    from apps.slack import commands as slack_commands
    from apps.slack.models import SlackInstallation

    agent = _agent_for_admin(request, slug)
    agent.slack_enabled = payload.slack_enabled
    agent.save(update_fields=["slack_enabled", "updated_at"])
    result = slack_commands.sync_quietly(SlackInstallation.objects.filter(links__workspace=agent.workspace).first())
    detail = result.get("detail", "")
    if result["status"] == "synced":
        name = slack_commands.command_name(agent.slug)
        if name is None:
            detail = f"`{agent.slug}` is too long to be a Slack command; use `/canopy {agent.slug}`."
        elif name in result.get("added", []):
            detail = f"Added {name} to Slack."
        elif name in result.get("removed", []):
            detail = f"Removed {name} from Slack."
    return SlackEnabledOut(slack_enabled=agent.slack_enabled, command_status=result["status"],
                           command_detail=detail)


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


@router.get("/{slug}/default-order", response=AgentDefaultOrderOut,
            summary="What the agent's work runs on when no rule matches")
def get_agent_default_order(request: HttpRequest, slug: str) -> AgentDefaultOrderOut:
    """Its own ordered runner list when it has one; otherwise the default order of
    its workspace, or of the nearest workspace above it that has one, which the
    agent follows live. Saving an empty list to `PUT /runners` makes an agent
    follow its workspace again; its source rules are unaffected either way."""
    from apps.harness.services import agents_with_own_order, default_order_source, inherited_orders

    agent = _get_agent_or_404(request, slug)
    if agent.pk in agents_with_own_order([agent.pk]):
        # `workspace` still names the order it WOULD follow, so a screen can
        # offer "follow it instead".
        return AgentDefaultOrderOut(own=True, workspace=default_order_source(agent),
                                    repo_url=agent.repo_url or "")
    inh = inherited_orders([agent.pk]).get(agent.pk)
    if inh is None:
        return AgentDefaultOrderOut(own=False, repo_url=agent.repo_url or "")
    return AgentDefaultOrderOut(
        own=False,
        workspace=inh.workspace,
        runners=[
            AgentRunnerOut(runner_id=r.pk, runner_name=r.name, kind=r.kind, rank=i,
                           online=r.live_status == r.ONLINE, ready=r.ready)
            for i, r in enumerate(inh.runners)
        ],
        missing_repo=[r.name for r in inh.missing_repo],
        cannot_hold=[r.name for r in inh.cannot_hold],
        repo_url=agent.repo_url or "",
    )


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

    agent = _agent_for_write(request, slug)

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
    # (owned by someone else, wrong tenant) must 422 as "unknown", never be
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
    _refuse_runners_that_cannot_hold(agent, runners)
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
        .select_related("enqueued_by", "initiator_contact")
        .only("origin", "origin_ref", "enqueued_by", "initiator_contact")
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
            turn_mode=row.turn_mode,
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

    agent = _agent_for_write(request, slug)

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

    # `auto` on a rule is the agent's admins' to set (docs/architecture/access.md).
    # Asked before the runners are, so an editor is told the actual reason.
    for r, actor in zip(payload.rules, actors):
        if r.turn_mode == "auto":
            _refuse_auto_unless_admin(request, agent, f"a routing rule ({r.source}/{actor or 'anyone'})")

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
    _refuse_runners_that_cannot_hold(agent, runners)

    with transaction.atomic():
        RunnerAssignment.objects.filter(agent=agent).exclude(source="").delete()
        RunnerAssignment.objects.bulk_create([
            # `rank` is the runner's position WITHIN its rule — the list order the
            # caller sent. `strict` is rule-level and written to every row of the
            # rule, so the composer can read it off the first.
            RunnerAssignment(
                agent=agent, runner=by_id[row.runner_id], rank=rank,
                source=r.source, actor=actor, strict=r.strict, enabled=row.enabled,
                turn_mode=r.turn_mode,
            )
            for r, actor in zip(payload.rules, actors)
            for rank, row in enumerate(r.runners)
        ])
    return list_agent_runner_rules(request, slug)


# ---- per-person routing (the additive face of the actor rules above) ----
#
# The same RunnerAssignment rows as /runner-rules, addressed by PERSON instead of
# wholesale. Two things made "route Alice to my laptop" impractical there:
#   1. a person spans five sources, so it took five hand-built rules; and
#   2. the PUT replaces every rule and requires the caller to have paired every
#      runner it names — so anyone but the operator who owns ALL the boxes got a
#      422 just for re-sending the rules already there.
# These routes touch one actor's rows only, and gate on the runners being NAMED.

def _actor_or_422(actor: str) -> str:
    from apps.harness.actors import normalize_actor

    normalized = normalize_actor(actor)
    if not normalized:
        raise HttpError(422, f"not an email address: {actor!r}")
    return normalized


def _actor_sources() -> list[str]:
    """Every routable source whose turns carry a person. The scheduler is the
    one that doesn't (`apps/harness/actors.py`), so an actor rule on it is dead."""
    from typing import get_args

    from apps.harness.models import Turn
    from apps.harness.schemas import RoutableSource

    return [s for s in get_args(RoutableSource) if s != Turn.ORIGIN_CANOPY_SCHEDULER]


@router.get("/{slug}/actor-routes", response=list[AgentActorRouteOut],
            summary="List which people's work routes to which runners")
def list_agent_actor_routes(request: HttpRequest, slug: str) -> list[AgentActorRouteOut]:
    """Per-person routing for this agent: for each person with a rule, the runners
    their work goes to (in preference order), whether that is strict, and on which
    sources. People with no entry follow the agent's source rules and default
    runner order."""
    rules = list_agent_runner_rules(request, slug)
    # (actor, source) -> rows of that rule, rank order (the list is pre-sorted).
    by_rule: dict[tuple[str, str], list[AgentRunnerRuleOut]] = {}
    for row in rules:
        if row.actor:
            by_rule.setdefault((row.actor, row.source), []).append(row)

    grouped: dict[tuple, AgentActorRouteOut] = {}
    for (actor, source), rows in by_rule.items():
        key = (actor, tuple((r.runner_id, r.enabled) for r in rows), rows[0].strict, rows[0].turn_mode)
        route = grouped.get(key)
        if route is None:
            route = grouped[key] = AgentActorRouteOut(
                actor=actor,
                runners=[
                    AgentActorRouteRunnerOut(
                        runner_id=r.runner_id, runner_name=r.runner_name,
                        online=r.online, enabled=r.enabled,
                    )
                    for r in rows
                ],
                strict=rows[0].strict, sources=[], turn_mode=rows[0].turn_mode,
            )
        route.sources.append(source)
        route.queued_count += rows[0].queued_count
    return sorted(grouped.values(), key=lambda r: (r.actor, r.sources))


@router.put("/{slug}/actor-routes/{actor}", response=list[AgentActorRouteOut],
            summary="Route one person's work to specific runners")
def set_agent_actor_route(
    request: HttpRequest, slug: str, actor: str, payload: AgentActorRouteIn
) -> list[AgentActorRouteOut]:
    """Send everything this person asks this agent to do (by email, Slack, chat,
    ace-web or API) to the given runners, in preference order. Replaces only THIS
    person's routing; everyone else's rules are untouched.

    `actor` is the person's email. With `strict` (the default) only the named
    runners may take their work — if those are offline it waits; with
    `strict=false` it falls back to the agent's usual runners after a minute.
    You may name only runners you administer (you own it, or its owner
    granted you admin) — this decides where work runs, so it is gated on the box,
    not just the agent. Remove a route with DELETE on the same path.
    """
    from apps.harness import services as hsvc
    from apps.harness.api import _runner_read_q
    from apps.harness.models import Runner, RunnerAssignment

    agent = _agent_for_write(request, slug)
    actor = _actor_or_422(actor)
    if payload.turn_mode == "auto":
        _refuse_auto_unless_admin(request, agent, f"routing {actor}'s work")

    sources = list(dict.fromkeys(payload.sources or _actor_sources()))
    dead = [s for s in sources if s not in _actor_sources()]
    if dead:
        raise HttpError(422, f"no person is behind {', '.join(dead)} turns, so a person's route can't apply there")
    if not payload.runners:
        raise HttpError(422, "a route needs at least one runner — DELETE removes a route")
    ids = [row.runner_id for row in payload.runners]
    if len(ids) != len(set(ids)):
        raise HttpError(422, "a runner may appear once per route")

    runners = {
        r.id: r for r in Runner.objects.filter(id__in=ids)
        .exclude(status=Runner.RETIRED).filter(_runner_read_q(request))
    }
    missing = [str(rid) for rid in ids if rid not in runners]
    if missing:
        raise HttpError(422, f"unknown or retired runner id(s): {', '.join(missing)}")
    # The tier that decides what a box does, not the one that speaks AS it: the
    # operator of a box paired under an agent's identity holds an admin grant, not
    # the pairing, and must still be able to point work at their own box.
    refused = [r.name for r in runners.values() if not hsvc.can_administer_runner(request.user, r)]
    if refused:
        raise HttpError(
            403,
            f"you don't administer runner(s) {', '.join(refused)} — ask its owner to "
            "grant you admin (POST /api/harness/runners/{id}/admins)",
        )
    _refuse_runners_that_cannot_hold(agent, runners.values())

    with transaction.atomic():
        RunnerAssignment.objects.filter(agent=agent, actor=actor).exclude(source="").delete()
        RunnerAssignment.objects.bulk_create([
            RunnerAssignment(
                agent=agent, runner=runners[row.runner_id], rank=rank, source=source,
                actor=actor, strict=payload.strict, enabled=row.enabled,
                turn_mode=payload.turn_mode,
            )
            for source in sources
            for rank, row in enumerate(payload.runners)
        ])
    return [r for r in list_agent_actor_routes(request, slug) if r.actor == actor]


@router.delete("/{slug}/actor-routes/{actor}", response={204: None},
               summary="Remove one person's routing")
def delete_agent_actor_route(request: HttpRequest, slug: str, actor: str):
    """Stop routing this person's work specially: their turns go back to the
    agent's source rules and default runner order. Touches no one else's rules.
    Idempotent — removing a person with no route is a 204."""
    from apps.harness.models import RunnerAssignment

    agent = _agent_for_write(request, slug)
    actor = _actor_or_422(actor)
    RunnerAssignment.objects.filter(agent=agent, actor=actor).exclude(source="").delete()
    return Status(204, None)


# ---- syncs (Google-Doc backed) ----
@router.get("/{slug}/syncs/", response=Page[AgentSyncOut], summary="List the agent's syncs",)
def list_syncs(request: HttpRequest, slug: str, limit: int = 100) -> Page[AgentSyncOut]:
    limit = clamp_limit(limit)
    agent = _get_agent_or_404(request, slug)
    items = [AgentSyncOut.model_validate(s) for s in services.list_syncs(agent, limit=limit)]
    return paginate(items, offset=0, limit=limit)


@router.post("/{slug}/syncs/", response={201: AgentSyncOut},
             summary="Post a Google-Doc sync (idempotent per period+source)",)
def create_sync(request: HttpRequest, slug: str, payload: AgentSyncIn) -> Status:
    agent = _agent_for_write(request, slug)
    sync = services.upsert_sync(agent, payload)
    return Status(201, AgentSyncOut.model_validate(sync))


@router.delete("/{slug}/syncs/{sync_id}/", response={204: None},
               summary="Delete a sync (wrong period / stray record)",)
def delete_sync(request: HttpRequest, slug: str, sync_id: int) -> Status:
    """POST upserts per (period, source), so re-posting only corrects a sync for the
    SAME window — a sync filed under the wrong period is otherwise unreachable."""
    agent = _agent_for_write(request, slug)
    if not services.delete_sync(agent, sync_id):
        raise HttpError(404, f"sync {sync_id} not found for agent '{slug}'")
    return Status(204, None)


# ---- turns (a packaged unit of work + optional transcript link) ----
@router.get("/{slug}/turns/", response=Page[AgentTurnOut], summary="List the agent's turns",)
def list_turns(request: HttpRequest, slug: str, limit: int = 100) -> Page[AgentTurnOut]:
    limit = clamp_limit(limit)
    agent = _get_agent_or_404(request, slug)
    from apps.harness import turn_access

    # Everyone sees what the agent did; a turn's prompt and transcript link are
    # a log (turn_access).
    turns = turn_access.redact(list(services.list_turns(agent, limit=limit)), request.user)
    items = [AgentTurnOut.model_validate(t) for t in turns]
    return paginate(items, offset=0, limit=limit)


@router.post("/{slug}/turns/", response={201: AgentTurnOut},
             summary="Package a turn (idempotent per cli_session_id)",)
def create_turn(request: HttpRequest, slug: str, payload: AgentTurnIn) -> Status:
    agent = _agent_for_write(request, slug)
    turn = services.upsert_turn(agent, payload)
    return Status(201, AgentTurnOut.model_validate(turn))


# ---- work products ----
@router.get("/{slug}/work-products/", response=Page[AgentWorkProductOut],
            summary="List the agent's work products",)
def list_work_products(request: HttpRequest, slug: str, limit: int = 200) -> Page[AgentWorkProductOut]:
    limit = clamp_limit(limit)
    agent = _get_agent_or_404(request, slug)
    items = [AgentWorkProductOut.model_validate(w) for w in services.list_work_products(agent, limit=limit)]
    return paginate(items, offset=0, limit=limit)


@router.post("/{slug}/work-products/", response=CountOut,
             summary="Add/update work products (upsert by url)",)
def add_work_products(request: HttpRequest, slug: str, payload: AgentWorkProductBatchIn) -> CountOut:
    agent = _agent_for_write(request, slug)
    result = services.upsert_work_products(agent, payload.work_products)
    return CountOut(**result)


# ---- skill catalog ----
@router.get("/{slug}/skills/", response=list[AgentSkillOut], summary="List the agent's skill catalog",)
def list_skills(request: HttpRequest, slug: str) -> list[AgentSkillOut]:
    agent = _get_agent_or_404(request, slug)
    return [AgentSkillOut.model_validate(s) for s in services.list_skills(agent)]


@router.put("/{slug}/skills/", response=CountOut, summary="Replace the agent's skill catalog",)
def replace_skills(request: HttpRequest, slug: str, payload: AgentSkillCatalogIn) -> CountOut:
    agent = _agent_for_write(request, slug)
    count = services.replace_skills(agent, payload.skills)
    return CountOut(count=count)


# ---- skill history ----
# Reading syncs first when the stored history is over an hour old: opening the
# page is the trigger (no scheduler exists, and a stale history costs one
# click). The sync itself is debounced per agent, so many open tabs clone once.
@router.get("/{slug}/skill-history/", response=SkillHistoryOut,
            summary="How the agent's skills changed, from its repository's history")
def get_skill_history(request: HttpRequest, slug: str) -> SkillHistoryOut:
    agent = _get_agent_or_404(request, slug)
    # Due = stale AND no attempt in the last hour: a FAILED attempt never moves
    # synced_at, so without the attempt debounce a repo_not_granted agent would
    # refresh the owner's token and clone on every page load. An owner who has
    # just fixed access presses Sync, which forces.
    if history.due_for_auto_sync(agent):
        try:
            history.sync(agent)
        except Exception:
            # A read must not 500 because the refresh behind it broke — serve
            # what is stored. The attempt is already stamped (see `_claim`),
            # so a deterministic failure is not retried on every load either.
            logger.exception("skill history auto-sync failed for agent %s", agent.slug)
    return SkillHistoryOut(**history.history_payload(agent, request.user))


@router.post("/{slug}/skill-history/sync", response=SkillHistoryOut,
             summary="Re-read the agent's skill history from its repository now")
def sync_skill_history(request: HttpRequest, slug: str) -> SkillHistoryOut:
    agent = _agent_for_write(request, slug)
    history.sync(agent, force=True)
    return SkillHistoryOut(**history.history_payload(agent, request.user))


@router.get("/{slug}/skill-history/revisions", response=dict[str, Any],
            summary="Skill revisions, newest first, with their commit messages")
def skill_history(
    request: HttpRequest, slug: str, skill: str | None = None, group: str | None = None,
    since: dt.date | None = None, until: dt.date | None = None, limit: int | None = None,
    commit: str | None = None,
) -> dict:
    """How an agent's skills changed, from its repository's git history.

    Returns revisions newest first: date, skill, commit subject AND body (the
    body is where the reason for a change is usually spelled out), lines after,
    and the line change. For a single skill it also names the QA/eval skills
    that check it (`checked_by`) or the skill it checks (`checks`).

    Filters: `skill` name, `group` title (a phase or agent from the History
    page), `commit` (a sha or sha prefix, 7 to 64 hex characters),
    `since` / `until` dates.

    Returns the 25 most recent matches by default; `limit` raises that, capped
    at 300. In a list each body is summarised to its first 700 characters and
    `body_truncated` says so — ask for that one `commit` to read it whole. A
    mature skill has hundreds of revisions and its bodies run to thousands of
    words each, so narrow with `skill`, `since`/`until` or `commit` rather than
    asking for everything.

    On the History page, read `current_page` first: it says which skill, group
    or commit is selected and the date being looked at. A selected commit's sha
    is `commit`; the page's `as_of` date is `until`.
    """
    agent = _get_agent_or_404(request, slug)
    try:
        return history.skill_revisions(
            agent, skill=skill, group=group, since=since, until=until, limit=limit, commit=commit,
        )
    except history.SyncError as e:  # a malformed commit
        raise HttpError(422, str(e)) from e


@router.get("/{slug}/skill-history/diff", response=dict[str, Any],
            summary="The change one commit made to one skill, as a unified diff")
def skill_revision_diff(request: HttpRequest, slug: str, sha: str, skill: str) -> dict:
    """The exact change one commit made to one skill's SKILL.md, as a unified diff.

    Fetched live from GitHub (not stored), truncated to 20 KB. Use it when a
    commit message does not say enough about what actually changed.
    """
    import requests

    from apps.tokens.github_app import GitHubAuthError, GitHubNotConfigured

    agent = _get_agent_or_404(request, slug)
    try:
        return history.revision_diff(agent, sha, skill)
    except history.SyncError as e:
        raise HttpError(422, str(e)) from e
    except (GitHubAuthError, GitHubNotConfigured) as e:
        raise HttpError(409, str(e)) from e
    except requests.RequestException as e:
        raise HttpError(502, f"GitHub: {e}") from e


# ---- tasks (board) ----
# ---- projects (the work a `Projects/<name>` Drive folder holds) ----


def _get_project_or_404(agent, ref: str):
    project = services.get_project(agent, ref)
    if project is None:
        raise HttpError(404, f"project {ref} not found")
    return project


@router.get("/{slug}/projects/", response=list[AgentProjectOut],
            summary="List the agent's projects",)
def list_projects(request: HttpRequest, slug: str, status: str = "") -> list[AgentProjectOut]:
    agent = _get_agent_or_404(request, slug)
    projects = services.list_projects(agent, status=status)
    counts = services.project_task_counts(agent)
    for project in projects:
        project._task_count, project._open_task_count = counts.get(project.pk, (0, 0))
    return [AgentProjectOut.model_validate(p) for p in projects]


@router.post("/{slug}/projects/", response={201: AgentProjectOut}, summary="Create a project",)
def create_project(request: HttpRequest, slug: str, payload: AgentProjectIn) -> Status:
    agent = _agent_for_write(request, slug)
    return Status(201, AgentProjectOut.model_validate(services.create_project(agent, payload)))


@router.get("/{slug}/projects/{ref}/", response=AgentProjectOut, summary="Get one project",)
def get_project(request: HttpRequest, slug: str, ref: str) -> AgentProjectOut:
    agent = _get_agent_or_404(request, slug)
    return AgentProjectOut.model_validate(_get_project_or_404(agent, ref))


@router.patch("/{slug}/projects/{ref}/", response=AgentProjectOut, summary="Update a project",)
def patch_project(request: HttpRequest, slug: str, ref: str,
                  payload: AgentProjectPatch) -> AgentProjectOut:
    agent = _agent_for_write(request, slug)
    project = _get_project_or_404(agent, ref)
    data = payload.model_dump(exclude_unset=True)
    if "links" in data and data["links"] is not None:
        data["links"] = [link if isinstance(link, dict) else link.model_dump()
                         for link in data["links"]]
    return AgentProjectOut.model_validate(services.patch_project(project, data))


@router.get("/{slug}/tasks/", response=list[AgentTaskOut], summary="List the agent's tasks (board)",)
def list_tasks(request: HttpRequest, slug: str) -> list[AgentTaskOut]:
    agent = _get_agent_or_404(request, slug)
    return [AgentTaskOut.model_validate(t) for t in services.list_tasks(agent)]


@router.get("/{slug}/tasks/waiting/", response=list[AgentTaskOut],
            summary="This agent's tasks waiting on you",)
def list_waiting_tasks(request: HttpRequest, slug: str) -> list[AgentTaskOut]:
    """The inbox, per agent: tasks parked on the CALLER.

    Routed on `waiting_on_user`, never on the free-text `assigned`: canopy
    cannot notify a string, and the fleet's boards spell one person three ways
    ("Jonathan", "Jonathan Jackson", "jjackson@dimagi.com").
    """
    agent = _get_agent_or_404(request, slug)
    return [AgentTaskOut.model_validate(t)
            for t in services.tasks_waiting_on(request.user, agent=agent)]


@router.post("/{slug}/tasks/sync", response=CountOut,
             summary="Upsert the agent's tasks from the (legacy) source sheet",)
def sync_tasks(request: HttpRequest, slug: str, payload: AgentTaskSyncIn) -> CountOut:
    agent = _agent_for_write(request, slug)
    return CountOut(**services.sync_tasks(agent, payload.tasks))


def _get_task_or_404(agent, task_id: int):
    task = services.get_task(agent, task_id)
    if task is None:
        raise HttpError(404, f"task {task_id} not found")
    return task


@router.post("/{slug}/tasks/", response={201: AgentTaskOut}, summary="Create a task",)
def create_task(request: HttpRequest, slug: str, payload: AgentTaskIn) -> Status:
    agent = _agent_for_write(request, slug)
    return Status(201, AgentTaskOut.model_validate(services.create_task(agent, payload)))


@router.patch("/{slug}/tasks/{task_id}/", response=AgentTaskOut, summary="Update a task",)
def patch_task(request: HttpRequest, slug: str, task_id: int, payload: AgentTaskPatch) -> AgentTaskOut:
    agent = _agent_for_write(request, slug)
    task = _get_task_or_404(agent, task_id)
    data = payload.model_dump(exclude_unset=True)
    try:
        return AgentTaskOut.model_validate(services.patch_task(task, data))
    except services.UnknownPersonError as exc:
        raise HttpError(422, str(exc)) from exc


# ---- task commands (the board's action queue) ----
#
# Which command kinds are a RESHAPE rather than an interaction. `PATCH
# /tasks/{id}/` is gated at `_agent_for_write`, and `kind: "edit"` performs the
# identical mutation through `services.create_command` — so gating one and not
# the other left the gate with a door beside it. A viewer refused on the PATCH
# could rewrite title/next_action/plan/owner/assigned via the command queue.
#
# The line is the role ladder's own: `accept` / `decline` / `comment` are
# DECIDING an item that is already on the board, which is what the User tier
# exists for ("decide an item, read the board"). `edit` rewrites the task's
# text, `reassign` moves who holds it, `done` declares the work finished, and
# `dispatch` queues fresh agent work — all reshapes, all editor.
_RESHAPING_COMMAND_KINDS = frozenset({
    AgentTaskCommand.EDIT,
    AgentTaskCommand.REASSIGN,
    AgentTaskCommand.DONE,
    AgentTaskCommand.DISPATCH,
})


@router.post("/{slug}/tasks/{task_id}/commands", response={201: CommandResultOut},
             summary="Post a board action (accept/decline/dispatch/…) on a task",)
def post_command(request: HttpRequest, slug: str, task_id: int, payload: AgentTaskCommandIn) -> Status:
    if payload.kind in _RESHAPING_COMMAND_KINDS:
        agent = _agent_for_write(request, slug)
    else:
        agent = _get_agent_or_404(request, slug)
    task = _get_task_or_404(agent, task_id)
    created_by = payload.created_by or getattr(request.user, "email", "")
    cmd = services.create_command(agent, task, payload.kind, payload.payload, created_by)
    return Status(201, CommandResultOut(
        command=AgentTaskCommandOut.model_validate(cmd),
        task=AgentTaskOut.model_validate(cmd.task) if cmd.task_id else None,
    ))


@router.get("/{slug}/commands", response=list[AgentTaskCommandOut],
            summary="List commands (the agent reads ?status=pending)",)
def list_commands(request: HttpRequest, slug: str, status: str | None = None) -> list[AgentTaskCommandOut]:
    agent = _get_agent_or_404(request, slug)
    return [AgentTaskCommandOut.model_validate(c) for c in services.list_commands(agent, status)]


@router.post("/{slug}/commands/{cmd_id}/apply", response=AgentTaskCommandOut,
             summary="Mark a command applied (the agent calls this after acting)",)
def apply_command(request: HttpRequest, slug: str, cmd_id: int, payload: AgentCommandApplyIn) -> AgentTaskCommandOut:
    # Marking a command applied tells the agent it is done, so the agent never
    # acts on it — the agent's own login or the reshaping tier, not any viewer.
    agent = _get_agent_or_404(request, slug)
    if not (agent.user_id is not None and agent.user_id == request.user.pk):
        agent = _agent_for_write(request, slug)
    cmd = agent.commands.filter(id=cmd_id).select_related("task", "agent").first()
    if cmd is None:
        raise HttpError(404, f"command {cmd_id} not found")
    return AgentTaskCommandOut.model_validate(services.apply_command(cmd, payload.result_note))


# ---- Agent credentials (spec 2026-09-05-agent-credentials-design) -----------

@router.put("/{slug}/credentials", response=list[AgentCredentialStatusOut],
            summary="Set named secrets for an agent (write-only)")
def set_agent_credentials(request: HttpRequest, slug: str, payload: AgentCredentialsIn):
    """Upsert. Non-clobbering: a ref absent from the body is untouched.

    There is no read counterpart on purpose — the response is the MASKED status,
    so even the caller who just wrote a value cannot read one back through the
    browser."""
    agent = _agent_for_admin(request, slug)
    try:
        services.set_agent_credentials(agent, payload.values, user=request.user)
    except ValueError as exc:
        raise HttpError(422, str(exc)) from exc
    return services.agent_credential_status(agent)


@router.get("/{slug}/credentials/status", response=list[AgentCredentialStatusOut],
            summary="Which declared refs are set (masked — booleans, never values)")
def agent_credential_status(request: HttpRequest, slug: str):
    """The question this answers is 'what is stopping this agent from running',
    which today requires SSH-ing to a box and reading a keyring."""
    agent = _get_agent_or_404(request, slug)
    return services.agent_credential_status(agent)


@router.get("/{slug}/credentials/resolve", response=AgentCredentialsResolveOut,
            summary="PLAINTEXT — a runner stages this agent's secrets")
def resolve_agent_credentials(request: HttpRequest, slug: str):
    """The one route that returns values, and it is not for a browser.

    Two gates, both required:

    1. **Bearer only.** A session cookie is refused even for the owner. That is
       what makes "the browser never sees plaintext" a property of the system
       rather than a habit of the UI — a future page cannot accidentally acquire
       the ability to render a secret.
    2. **The caller must pair a live runner this agent routes to.** Tighter than
       workspace membership on purpose: plaintext should reach a box that runs
       the agent, not everyone who can see it. Mirrors the runner credential
       fetch, whose boundary is "the caller who can claim turns as this runner".

    Every read is recorded, so a credential fetch is visible in the fleet log
    rather than silent.
    """
    if not request.META.get("HTTP_AUTHORIZATION", "").startswith("Bearer "):
        raise HttpError(403, "resolve requires a bearer token; a browser session is never given values")

    agent = _get_agent_or_404(request, slug)
    if not services.caller_runs_agent(request.user, agent):
        raise HttpError(403, "no live runner you pair is assigned to this agent")

    values = services.resolve_agent_credentials(agent)
    vault, op_token = services.resolve_agent_vault(agent)
    shared_vault, shared_token = services.resolve_shared_vault(agent)
    try:
        from apps.events import services as events

        events.record(
            [{
                "source": "agents.credentials",
                "kind": "agent.credentials.resolved",
                "level": "info",
                "key": f"{agent.slug}:{request.user.pk}",
                "summary": f"{len(values)} secret(s) resolved for {agent.slug}",
                "payload": {"agent": agent.slug, "count": len(values)},
            }],
            workspace=agent.workspace,
        )
    except Exception:  # noqa: BLE001 - an audit hiccup must not deny a runner its secrets
        pass
    delegation = delegations.delegation_for(agent)
    return AgentCredentialsResolveOut(
        values=values, op_vault=vault, op_sa_token=op_token,
        shared_op_vault=shared_vault, shared_op_sa_token=shared_token,
        github_token=delegations.decrypt_secret(delegation.secret_enc) if delegation else "",
        mailbox=agent.email or "",
    )


@router.get("/{slug}/vault", response=AgentVaultOut,
            summary="This agent's 1Password vault (masked — never the key)")
def get_agent_vault(request: HttpRequest, slug: str) -> AgentVaultOut:
    agent = _get_agent_or_404(request, slug)
    return services.agent_vault_status(agent)


@router.put("/{slug}/vault", response=AgentVaultOut,
            summary="Set the vault + its service-account token (write-only)")
def set_agent_vault(request: HttpRequest, slug: str, payload: AgentVaultIn) -> AgentVaultOut:
    """The key is scoped to ONE agent's vault by design.

    A single fleet-wide token would be simpler to operate and would make
    canopy-web worth attacking for every agent's secrets at once; this bounds a
    compromise to the one agent whose key was taken (Jonathan, 2026-09-06)."""
    agent = _agent_for_admin(request, slug)
    return services.set_agent_vault(
        agent, vault=payload.vault, service_key=payload.service_key,
    )


# ---- GitHub: the owner's identity, lent to this agent -------------------------
# The owner pastes a fine-grained token here; a runner is handed it one turn at a
# time. See apps/agents/delegations.py for the rule and AgentDelegation for why it
# is not a vault item.

@router.get("/{slug}/github", response=AgentGitHubOut,
            summary="How this agent acts on GitHub (masked — never the token)")
def get_agent_github(request: HttpRequest, slug: str) -> AgentGitHubOut:
    """Whose GitHub identity this agent's pull requests use, when that token
    expires, and whether it can open a pull request on the agent's repo."""
    agent = _get_agent_or_404(request, slug)
    return AgentGitHubOut(**delegations.status(agent))


@router.put("/{slug}/github", response=AgentGitHubOut,
            summary="Lend this agent your GitHub identity (owner only, write-only)")
def set_agent_github(request: HttpRequest, slug: str, payload: AgentGitHubIn) -> AgentGitHubOut:
    """Store the owner's fine-grained GitHub token for this agent.

    Checked before it is stored: GitHub must accept it, and it must be able to
    open a pull request on the agent's own repo. A token that fails is refused
    with the reason, never saved."""
    agent = _get_agent_or_404(request, slug)
    try:
        delegations.set_github(agent, request.user, payload.token)
    except delegations.DelegationError as exc:
        raise HttpError(422, str(exc)) from exc
    return AgentGitHubOut(**delegations.status(agent))


@router.post("/{slug}/github/check", response=AgentGitHubOut,
             summary="Re-check this agent's GitHub token against GitHub")
def check_agent_github(request: HttpRequest, slug: str) -> AgentGitHubOut:
    agent = _get_agent_or_404(request, slug)
    delegations.check_github(agent)
    return AgentGitHubOut(**delegations.status(agent))


@router.delete("/{slug}/github", response=AgentGitHubOut,
               summary="Withdraw your GitHub identity from this agent")
def delete_agent_github(request: HttpRequest, slug: str) -> AgentGitHubOut:
    """Removes the CALLER's own delegation — nobody can withdraw someone
    else's, and an agent admin who is not its owner has none to withdraw."""
    agent = _get_agent_or_404(request, slug)
    delegations.clear_github(agent, request.user)
    return AgentGitHubOut(**delegations.status(agent))


# Registered AFTER the literal `status`/`resolve` paths on purpose: Django
# resolves in order, so a `{name}` pattern declared first swallows "status" and
# answers 405 Method Not Allowed — the route exists, it is simply unreachable.
@router.delete("/{slug}/credentials/{name}", response=list[AgentCredentialStatusOut],
               summary="Remove one named secret")
def delete_agent_credential(request: HttpRequest, slug: str, name: str):
    agent = _agent_for_admin(request, slug)
    services.delete_agent_credential(agent, name)
    return services.agent_credential_status(agent)


@router.get("/{slug}/readiness", response=list[BootstrapReportOut],
            summary="What each BOX reports it could actually materialize")
def agent_readiness(request: HttpRequest, slug: str) -> list[BootstrapReportOut]:
    """The counterpart to `credentials/status`, and the difference is the point.

    `status` answers "is the credential stored here". This answers "could the
    box USE it" — and on 2026-09-07 those disagreed for a whole day: canopy-web
    held a valid gog-token while every gmail call on the box failed, because the
    OAuth client id+secret it needs alongside had not materialized. Nothing
    outside journald could see that.

    `mailbox_ok` alone repeated the same shape one layer up on 2026-09-08: it
    was TRUE for a day while every ACE email turn blocked at preflight, because
    the box verifies the client whose token AUTHENTICATES while a turn presents
    the client its config DECLARES. Read `turn_ready` for "can this agent run a
    turn"; `mailbox_ok` only says some client works.
    """
    agent = _get_agent_or_404(request, slug)
    return [
        BootstrapReportOut(
            runner_name=r.runner_name, client_creds_ok=r.client_creds_ok,
            mailbox_ok=r.mailbox_ok, gog_client=r.gog_client,
            turn_client=r.turn_client, turn_ready=r.turn_ready,
            env_ok=r.env_ok, detail=r.detail, reported_at=r.reported_at,
        )
        for r in services.bootstrap_reports(agent)
    ]


@router.post("/{slug}/bootstrap-report", response=BootstrapReportOut,
             summary="A box reports what it materialized for this agent")
def post_bootstrap_report(request: HttpRequest, slug: str,
                          payload: BootstrapReportIn) -> BootstrapReportOut:
    """Same gate as `credentials/resolve`: only a caller pairing a live runner
    this agent routes to may report for it. A readiness signal anyone could
    write is a readiness signal nobody can trust — and this one is meant to be
    trusted over the control plane's own record of what it stored.
    """
    # Membership + `caller_runs_agent`, and deliberately NOT `_agent_for_write`
    # on top: `caller_runs_agent` is strictly tighter than any role check, so a
    # role gate adds no security here — but it does add a way for readiness
    # reporting to start 403-ing, when a runner's pairing human happens to hold
    # `viewer`. A machine saying "I could not materialize this" is the last
    # signal to lose, and reporting what a box observed is not reshaping the
    # agent.
    agent = _get_agent_or_404(request, slug)
    if not services.caller_runs_agent(request.user, agent):
        raise HttpError(403, "no live runner you pair is assigned to this agent")
    r = services.record_bootstrap_report(
        agent, runner_name=payload.runner_name,
        client_creds_ok=payload.client_creds_ok, mailbox_ok=payload.mailbox_ok,
        gog_client=payload.gog_client, detail=payload.detail,
        turn_client=payload.turn_client, turn_ready=payload.turn_ready,
        env_ok=payload.env_ok,
    )
    return BootstrapReportOut(
        runner_name=r.runner_name, client_creds_ok=r.client_creds_ok,
        mailbox_ok=r.mailbox_ok, gog_client=r.gog_client,
        turn_client=r.turn_client, turn_ready=r.turn_ready,
        env_ok=r.env_ok, detail=r.detail, reported_at=r.reported_at,
    )

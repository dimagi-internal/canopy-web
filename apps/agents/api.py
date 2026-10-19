"""Django Ninja router for the /api/agents surface — a first-class AI-agent
workspace (agents, their Google-Doc syncs, projects, tasks, and skill catalog)."""
from __future__ import annotations

import datetime as dt
import logging
from typing import Any

import requests
from django.db import transaction
from django.db.models import Case, IntegerField, Q, When
from django.http import HttpRequest, HttpResponse, JsonResponse
from ninja import Router, Status
from ninja.errors import HttpError

from apps.api.auth import session_auth
from apps.api.errors import (
    TYPE_CONFLICT,
    TYPE_FORBIDDEN,
    TYPE_NOT_FOUND,
    TYPE_UPSTREAM,
    TYPE_VALIDATION,
    ProblemError,
)
from apps.api.pagination import Page, clamp_limit, paginate
from apps.harness.schemas import RunnerDeviceKeyOut
from apps.passkeys import stepup
from apps.plugins import services as plugin_services
from apps.workspaces import permissions as perms
from apps.workspaces import services as wsvc

from . import delegations, services
from . import skill_history as history
from .schemas import (
    ProposedTaskOut,
    AgentMoveIn,
    AgentMoveOut,
    AgentScaffoldIn,
    AgentScaffoldOut,
    AgentSetupStepOut,
    RepoCheckIn,
    RepoCheckOut,
    AgentInterfaceIn,
    AgentInterfaceOut,
    AgentAccessOut,
    AgentAdminOut,
    AgentCredentialsIn,
    AgentGitHubIn,
    AgentSalesforceIn,
    AgentSalesforceOut,
    AgentCanopyUserIn,
    AgentCanopyTokenIn,
    AgentCanopyLoginOut,
    AgentCanopyTokenOut,
    AgentGitHubOut,
    AgentCredentialAccessOut,
    AgentCredentialsResolveOut,
    AgentCredentialRevealOut,
    AgentCredentialStatusOut,
    AgentDetailOut,
    AgentIn,
    AgentOut,
    AgentPluginOut,
    AgentOwnerIn,
    AgentActorRouteIn,
    AgentActorRouteOut,
    AgentActorRouteRunnerOut,
    AgentDefaultOrderOut,
    AgentRunnerOut,
    AgentRunnerRowIn,
    AgentRunnerRuleOut,
    AgentRunnerRuleBodyIn,
    AgentRunnerRulesIn,
    AgentRunnersIn,
    AgentRuntimeOut,
    AgentSkillCatalogIn,
    AgentSkillOut,
    AgentSyncIn,
    AgentSyncOut,
    AgentTaskActionIn,
    AgentTaskActionOut,
    AgentTaskDetailOut,
    AgentTaskIn,
    AgentProjectDetailOut,
    AgentProjectIn,
    AgentProjectOut,
    AgentProjectPatch,
    AgentTaskOut,
    AgentTaskPatch,
    AgentTurnIn,
    AgentTurnOut,
    AgentVaultIn,
    AgentVaultOut,
    SenderTrustIn,
    SenderTrustOut,
    BootstrapReportIn,
    BootstrapReportOut,
    ActOut,
    ActionAppliedIn,
    CountOut,
    RunnerPreferenceIn,
    SkillHistoryOut,
    ProjectParticipantOut,
    SlackEnabledIn,
    SlackEnabledOut,
    TurnBriefOut,
    TurnModeIn,
    CredentialSourceIn,
    RepositoryIn,
    AgentCredentialReadOut,
    DeviceBindingIn,
    DeviceProofRequiredOut,
    AgentCredentialAlertOut,
    PatRotationIn,
    PatRotationOut,
    ZkEnableIn,
    ZkRotateIn,
    ZkStateOut,
    ZkValuesIn,
    ZkValuesOut,
    ZkWrapMissingOut,
    ZkWrapsIn,
)

logger = logging.getLogger(__name__)

router = Router(auth=session_auth, tags=["agents"])


def _visible_agent_workspace_ids(request: HttpRequest) -> set[str]:
    """The single definition of 'agent workspaces this caller can see'. A
    workspace_id is visible if the caller is pinned to it, or — unpinned —
    the caller is a member of it. _get_agent_or_404 builds from this, and
    list_agents and the fleet items query from `_listed_agent_workspace_ids`,
    which is this plus the read rule (a list never spans two tenant trees
    unpinned) — so a row a list shows is always one the by-slug gate admits.
    They used to hand-copy this
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
    whole agents surface — reads (tasks, projects, skills, turns,
    including AgentTurnOut.share_token, a public transcript link) AND writes
    (board commands, PUT /runners). Strictly broader than the read-only hole
    `_agent_or_404` (apps/harness/api.py, F1) closed for the same
    workspace-less-agent case. An unhomed agent must be unresolvable via this
    API, not universally visible. Since agents/0013 made `Agent.workspace` NOT
    NULL the case is unrepresentable rather than merely unpopulated, so this
    reads as a plain membership check with nothing to special-case."""
    return wsvc.request_member_slugs(request)


def _listed_agent_workspace_ids(request: HttpRequest) -> set[str]:
    """The agent workspaces a LIST spans (`list_agents`, the fleet task and
    project lists): `_visible_agent_workspace_ids` under the read rule — a
    caller whose workspaces sit in more than one tenant tree and who pins none
    gets a 422 rather than every organisation's agents in one list
    (`wsvc.read_scope`). By-slug gates keep `_visible_agent_workspace_ids`."""
    return wsvc.request_workspace_slugs(request)


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


def _runners_for_routing(request: HttpRequest, agent, ids, already) -> dict:
    """The runners a routing write names, by id — gated on the ones it ADDS.

    THE gate for every write that points an agent's work at boxes (the default
    list, a source rule, a person's route). A runner being ADDED must live where
    it could serve this agent (the agent's workspace or one above it — the
    fleet's boxes live in `dimagi`, its agents in `connect`), be one the caller
    ADMINISTERS (owns, or holds a grant on), and be trusted with the agent. A
    runner the write merely KEEPS (`already`: on this list or rule now) is taken
    as it is.

    Why keeping is free: routing is a shared object. ACE's rules name the fleet's
    cloud boxes (jjackson's) and Sarvesh's laptop (his), and when every runner was
    re-checked against the caller, NO one could save ACE's rules at all — not even
    unchanged (2026-10-05, #1143). The gate exists so a box can't be attached by
    someone without rights over it; re-sending a row someone with those rights
    already wrote attaches nothing.

    A box the caller neither administers nor can otherwise see answers "unknown",
    never "forbidden": a guessed UUID must not learn that a runner exists.
    """
    from apps.harness import services as hsvc
    from apps.harness.api import _runner_read_q
    from apps.harness.models import Runner

    ids = list(ids)
    already = set(already)
    added = [rid for rid in dict.fromkeys(ids) if rid not in already]
    live = Runner.objects.filter(id__in=ids).exclude(status=Runner.RETIRED)
    by_id = {r.id: r for r in live.filter(id__in=already)}

    home = [agent.workspace_id, *(agent.workspace.ancestor_slugs() if agent.workspace_id else [])]
    # ...or, with no workspace at all, the caller's own (a box with no tenant is
    # its owner's alone — the null leg `_runner_visibility_q` always allowed).
    candidates = list(live.filter(id__in=added).filter(
        Q(workspace_id__in=home) | Q(workspace_id__isnull=True, owner=request.user)))
    seen = set(Runner.objects.filter(id__in=[r.id for r in candidates])
               .filter(_runner_read_q(request)).values_list("id", flat=True))
    refused = []
    for r in candidates:
        if hsvc.can_administer_runner(request.user, r):
            by_id[r.id] = r
        elif r.id in seen:
            refused.append(r.name)
    missing = [str(rid) for rid in ids if rid not in by_id and rid not in
               {r.id for r in candidates if r.name in refused}]
    if missing:
        raise HttpError(422, f"unknown or retired runner id(s): {', '.join(missing)}")
    if refused:
        raise HttpError(
            403,
            f"you don't administer runner(s) {', '.join(sorted(refused))} — ask its owner to "
            "grant you admin (POST /api/harness/runners/{id}/admins)",
        )
    _refuse_runners_that_cannot_hold(agent, [by_id[rid] for rid in added])
    _refuse_other_units(agent, [by_id[rid] for rid in added])
    return by_id


def _refuse_other_units(agent, runners) -> None:
    """422 naming every runner whose isolation unit keeps it from this agent
    (`routing.unit_refusal`): an agent unit runs only its own agent, and an agent
    with a unit of its own runs on no other runner of that machine. The claim
    refuses them anyway; refusing the write says so while someone is looking."""
    from apps.harness.routing import unit_refusal

    said = sorted({why for why in (unit_refusal(r, agent.slug) for r in runners) if why})
    if said:
        raise HttpError(422, f"cannot route {agent.slug} there: {'; '.join(said)}")


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


def _plugin_out(agent, refs: dict[int, dict]) -> AgentPluginOut | None:
    ref = plugin_services.plugin_ref(agent, refs)
    return AgentPluginOut(**ref) if ref else None


@router.get("/", response=Page[AgentOut], summary="List agents",)
def list_agents(request: HttpRequest, limit: int = 100) -> Page[AgentOut]:
    limit = clamp_limit(limit)
    visible = _listed_agent_workspace_ids(request)
    agents = [a for a in services.list_agents() if a.workspace_id in visible]
    refs = plugin_services.plugin_refs(request.user) if agents else {}
    items = [
        AgentOut.model_validate(a).model_copy(update={"plugin": _plugin_out(a, refs)})
        for a in agents
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
    google = (payload.google_account or "").strip()
    if google and ("@" not in google or google.startswith("@") or google.endswith("@")):
        raise HttpError(422, "google_account is not an email address")
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
                "no unambiguous workspace to home this agent in; pass `workspace`: "
                + wsvc.creation_refusal(request),
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
    # Changing WHAT an existing agent is — the repo its boxes clone, the engine,
    # the secrets and sources they install — is holding its keys: whoever names
    # the repo writes the code that runs with the agent's credentials. So it is
    # the agent's admins', like its credentials. A field sent with the value
    # already stored is not a change: the plugin re-upserts on every publish.
    # Creating an agent, and editing its name/description/persona, stay editor.
    if existing is not None and not existing.is_admin(request.user):
        changed = [f for f in services.DEFINITION_FIELDS
                   if getattr(payload, f, None) is not None
                   and getattr(payload, f) != getattr(existing, f)]
        if changed:
            raise HttpError(
                403,
                f"changing {', '.join(changed)} on {existing.slug} requires being its "
                "owner or one of its admins",
            )

    # Whoever registers a NEW agent owns it — from the create wizard and from a terminal's
    # `/canopy:create-agent` alike. An ownerless agent cannot be lent a GitHub identity
    # (`delegations.set_github` is owner-only) and no box may hold it unless a workspace
    # owner pairs one, so leaving it unset left every new agent stuck until someone ran
    # `transfer_owner`. Applied on create only; an existing agent keeps its owner.
    agent = services.upsert_agent(payload, workspace=home, owner=request.user)
    if existing is None or (payload.repo_url is not None and payload.repo_url != existing.repo_url):
        # A cloud box only learns of an agent (or its new repo) by re-running its
        # bootstrap; ask the ones that will take its turns to do that now.
        from .setup import request_setup

        request_setup(agent)
    if explicit and agent.workspace_id != explicit:
        # Explicit home: MOVES an already-homed agent — the whole move, through
        # the same operation as POST /{slug}/move, so its turns, sessions, mail
        # and grants go with it. Setting the pointer alone left all of that in
        # the old tenant (#1532). The gate is the move's own (`agent_move.check`):
        # a missing workspace and a non-member get the same 404, and the
        # destination needs editor or above. A refusal raises inside this
        # route's transaction, so the fields written above roll back with it.
        _move(request, agent, explicit)
    if existing is None or (existing.email or "").lower() != (agent.email or "").lower():
        # An address on one of the workspace's canopy-hosted domains needs no sign-in:
        # give it its mailbox route now, so mail to it is the agent's turn from the first
        # message. Anything else (a Google address) is signed in later, as before.
        from . import mailbox

        mailbox.ensure_route(agent, by=request.user)
    # No `ensure_member` here any more, and it is not an omission: since the
    # role gate above, every path that reaches this line has already required
    # editor-or-owner in `agent.workspace` — on a create, on an edit, and in the
    # destination of a move. So the call could only ever be a no-op, while
    # reading as though registration were a way to join a tenant. That shape is
    # exactly what `wsvc.creation_workspace`'s docstring is about.
    return Status(201, AgentOut.model_validate(agent))


def _move(request: HttpRequest, agent, workspace: str, *, dry_run: bool = False) -> dict:
    """Gate, then dry-run or carry out a move (apps/agents/move.py), reporting the
    change to the settings audit. A dry run notes an unchanged value, so the audit
    records nothing for it."""
    from apps.events import audit

    from . import move as agent_move

    try:
        dest = agent_move.check(agent, workspace, by=request.user)
        source = agent.workspace
        plan = (agent_move.plan if dry_run else agent_move.move)(agent, dest, by=request.user)
    except agent_move.MoveError as exc:
        raise HttpError(exc.status, str(exc)) from None
    audit.note_change(scope="agent.workspace", before=source.slug,
                      after=source.slug if dry_run else dest.slug, workspace=source,
                      summary=f"{request.user.email} moved agent {agent.slug} from "
                              f"'{source.slug}' to '{dest.slug}'")
    return plan.as_dict(dry_run=dry_run)


@router.post("/{slug}/move", response=AgentMoveOut,
             summary="Move an agent, with its history, to another workspace")
def move_agent(request: HttpRequest, slug: str, payload: AgentMoveIn, dry_run: bool = False):
    """Re-home the agent and everything that is its history — turns, chat
    sessions, mail threads and messages, mail routes the destination can hold,
    capability grants and leases, agent-scoped admission and retention rules —
    in one transaction, or change nothing.

    `?dry_run=1` changes nothing and returns the same report: per-table row
    counts, what is deliberately left behind (people records, a person's own
    grants), and what will stop working (a mail address on a domain the
    destination cannot use, runners that do not serve it, a different shared
    vault or Slack). The move writes an event in both workspaces.

    Who: the agent's owner or one of its admins, with the editor role or above
    in the destination."""
    agent = _get_agent_or_404(request, slug)
    return _move(request, agent, payload.workspace, dry_run=dry_run)


# ---- creating an agent from the browser (apps/agents/creation.py) ----
# Two routes and then the ordinary upsert above: check the repo, push the scaffold,
# register. Each is idempotent, so a wizard (or an MCP client) that stops halfway
# just runs it again. Registered before the `/{slug}/…` routes so `scaffold` is
# never read as a slug.

#: A slug the SPA needs for itself: `/w/:ws/agents/new` is the create page.
_RESERVED_SLUGS = {"new", "scaffold"}


def _scaffold_slug(request: HttpRequest, workspace: str, raw_slug: str, repo: str) -> str:
    """The normalised slug, after the same gate `upsert_agent` applies to a create:
    a member (404 otherwise) with `agent.work` in the target workspace. A slug that
    is taken is refused unless it is THIS caller's agent on THIS repo — a retry."""
    from canopy_agent_factory import AgentFactoryError, normalize_slug

    from .creation import repo_url

    if not wsvc.is_member(request.user, workspace):
        raise ProblemError(404, f"workspace '{workspace}' not found", type_=TYPE_NOT_FOUND)
    if not _can(request, workspace, perms.AGENT_WORK):
        raise ProblemError(403, "creating an agent requires the editor role or above",
                           type_=TYPE_FORBIDDEN)
    try:
        slug = normalize_slug(raw_slug)
    except AgentFactoryError as exc:
        raise ProblemError(422, "That name cannot be an agent slug", type_=TYPE_VALIDATION,
                           detail=str(exc)) from None
    if slug in _RESERVED_SLUGS:
        raise ProblemError(422, "That name cannot be an agent slug", type_=TYPE_VALIDATION,
                           detail=f"'{slug}' is reserved; pick another")
    existing = services.get_agent(slug)
    if existing is not None and not (existing.is_admin(request.user)
                                     and existing.repo_url in ("", repo_url(repo))):
        # Slugs are global (they are Slack commands), so this cannot be avoided;
        # it says no more than "taken", whichever tenant holds it.
        raise ProblemError(409, f"The slug '{slug}' is taken", type_=TYPE_CONFLICT,
                           detail="Agent slugs are unique across canopy. Pick another name.")
    return slug


def _scaffold_target(request: HttpRequest, workspace: str, raw_slug: str, raw_repo: str):
    """(slug, owner/name) after the create gate, or a 4xx problem."""
    from . import creation

    try:
        full = creation.parse_repo(raw_repo)
    except creation.ScaffoldError as exc:
        raise ProblemError(exc.status, str(exc), type_=TYPE_VALIDATION) from None
    return _scaffold_slug(request, workspace, raw_slug, full), full


def _inspect(token: str, full: str, slug: str):
    """Who the token is and what the repo looks like through it, or a problem."""
    from . import creation

    try:
        who = delegations.inspect_github(token)
        return who, creation.inspect_repo(token, full, slug)
    except delegations.DelegationError as exc:
        raise ProblemError(422, str(exc), type_=TYPE_VALIDATION) from None
    except creation.ScaffoldError as exc:
        raise ProblemError(exc.status, str(exc), type_=TYPE_VALIDATION) from None
    except requests.RequestException as exc:
        raise ProblemError(502, "Could not reach GitHub", type_=TYPE_UPSTREAM, detail=str(exc)) from None


# POST, though it writes nothing: it carries a token, which does not belong in a URL.
@router.post("/scaffold/check", response=RepoCheckOut,
             summary="Can a new agent's scaffold be pushed into this repository with this token?")
def check_scaffold_repo(request: HttpRequest, payload: RepoCheckIn) -> RepoCheckOut:
    """Reads only, and stores nothing. `ready` means empty and the token can see it,
    `update` means it already holds this agent's scaffold (or only GitHub's starter
    files) and `files_to_add` template files are missing — only those are added, nothing
    is overwritten; `adopt` means nothing is missing; `name_mismatch` means it is a
    different agent (`suggested_slug`); `missing` means the token cannot see it;
    `not_empty` means it is not an agent repository. `token_url` is GitHub's token form."""
    from . import creation

    slug, full = _scaffold_target(request, payload.workspace, payload.slug, payload.repo)
    owner, name = full.split("/", 1)
    links = {"new_repo_url": creation.new_repo_url(owner, name),
             "token_url": delegations.token_form_url(slug, full)}
    if not payload.token.strip():
        return RepoCheckOut(repo=full, state="no_token", detail="Paste a token to check the repository.",
                            **links)
    who, state = _inspect(payload.token.strip(), full, slug)
    return RepoCheckOut(repo=full, state=state.state, default_branch=state.default_branch,
                        detail=state.detail, login=who["login"], files_to_add=len(state.missing),
                        suggested_slug=state.suggested_slug, **links)


@router.post("/scaffold", response={201: AgentScaffoldOut},
             summary="Push a new agent's scaffold into an empty repository")
def scaffold_agent(request: HttpRequest, payload: AgentScaffoldIn) -> Status:
    """Stamps the agent with the same factory `/canopy:create-agent` uses and pushes it into
    an empty repository you created (or adds just the missing files to one that already
    holds this agent's scaffold, overwriting nothing), with your fine-grained token. Stores nothing: follow
    with `POST /api/agents/` (`upsert_agent`) carrying the returned `repo_url`/`repo_ref`,
    then `PUT /api/agents/{slug}/github` with the same token so its turns use it too.
    Safe to repeat — a repository that already holds this scaffold is adopted."""
    from canopy_agent_factory import AgentSpec

    from . import creation

    slug, full = _scaffold_target(request, payload.workspace, payload.slug, payload.repo)
    token = payload.token.strip()
    _, state = _inspect(token, full, slug)
    if state.state in ("missing", "not_empty", "name_mismatch"):
        raise ProblemError(409, state.detail, type_=TYPE_CONFLICT)
    files = 0
    if state.state in ("ready", "update"):
        user = request.user
        rendered = creation.render(AgentSpec(
            slug=slug, display_name=payload.name.strip(), mandate=payload.mandate.strip(),
            mailbox=payload.mailbox.strip(), stakeholders=payload.stakeholders.strip(),
            author_name=user.get_full_name() or user.email, author_email=user.email,
        ))
        try:
            files = creation.push(token, state, rendered, slug=slug)
        except creation.ScaffoldError as exc:
            raise ProblemError(exc.status, str(exc),
                               type_=TYPE_CONFLICT if exc.status == 409 else TYPE_UPSTREAM) from None
        except requests.RequestException as exc:
            raise ProblemError(502, "Could not reach GitHub", type_=TYPE_UPSTREAM, detail=str(exc)) from None
    return Status(201, AgentScaffoldOut(
        repo=full, repo_url=creation.repo_url(full), repo_ref=state.default_branch,
        adopted=state.state == "adopt", files=files))


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
        "plugin": plugin_services.plugin_ref(agent, plugin_services.plugin_refs(request.user)),
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


# Returns a secret, once. Its caller is `canopy agent vault`, which writes it straight
# into the agent's 1Password vault; a fleet-baseline rail keeps agents from calling the
# MCP twin of this route, whose result would land in a transcript.
@router.post("/{slug}/canopy-token", response={201: AgentCanopyTokenOut},
             summary="Mint the agent's own canopy-web token (shown once)")
def mint_agent_canopy_token(request: HttpRequest, slug: str, payload: AgentCanopyTokenIn) -> Status:
    """For the agent's own login, creating it when the agent has none. See
    `services.mint_agent_canopy_token` for who may, and why it never adopts an
    existing account."""
    from apps.tokens.middleware import minting_refusal

    agent = _agent_for_admin(request, slug)
    refused = minting_refusal(request)
    if refused:
        raise HttpError(403, refused)
    try:
        raw, token, user, created = services.mint_agent_canopy_token(
            agent, request.user, email=payload.email, label=payload.label)
    except services.AgentTokenRefused as exc:
        raise HttpError(exc.status, str(exc)) from exc
    return Status(201, AgentCanopyTokenOut(raw=raw, token_id=token.pk, label=token.label,
                                           user_email=user.email, created_user=created))


# The browser's "Give it its canopy login": the same mint, but the token goes straight
# into the agent's Canopy vault (sealed) and is never returned — so, unlike the route
# above, nothing here can land in a transcript or a page.
@router.post("/{slug}/canopy-login", response={201: AgentCanopyLoginOut},
             summary="Give the agent its canopy login, stored in its Canopy vault")
def give_agent_canopy_login(request: HttpRequest, slug: str, payload: AgentCanopyTokenIn) -> Status:
    """Mint the agent's own canopy token, store it as its `canopy-pat` sealed to the
    people and machines that may read the agent, and turn on automatic rotation. Only
    for an agent on the Canopy vault (409 otherwise, or when nobody could open the
    stored token yet). Changes a secret, so a browser needs a fresh passkey step-up.
    The owner or an admin."""
    from apps.tokens.middleware import minting_refusal

    agent = _agent_for_admin(request, slug)
    refused = minting_refusal(request)
    if refused:
        raise HttpError(403, refused)
    if stepup.is_browser(request):
        stepup.require_recent_step_up(request)
    try:
        out = services.give_agent_canopy_login(agent, request.user, email=payload.email)
    except services.AgentTokenRefused as exc:
        raise HttpError(exc.status, str(exc)) from exc
    return Status(201, AgentCanopyLoginOut(**out))


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
    from .interface import domain_warnings

    by = agent.interface_published_by
    iface = agent.interface or {}
    return {"interface": iface, "source": agent.interface_source or "",
            "published_at": agent.interface_published_at,
            "published_by_email": by.email if by is not None else None,
            # Computed AFTER any save, cached and time-boxed: it never blocks one.
            "warnings": domain_warnings(iface, agent.workspace_id) if iface else []}


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
               summary="Unpublish the declared interface: only the agent's admins and its workspace's editors can reach it")
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
    syncs, projects, schedules, task actions, runner assignments/drills), so
    this is a real delete rather than a soft flag — nothing is left dangling.
    One thing blocks it: a workspace project the agent stewards (409). Its open chat sessions are closed first: the runner
    deletes each emdash task, and an offline runner does so when it is back.
    """
    from apps.canopy_sessions.services import close_agent_sessions

    agent = _agent_for_write(request, slug)
    # A workspace project it stewards is the workspace's, not the agent's: the
    # cascade would delete it. Hand it on first.
    stewarded = services.stewarded_workspace_projects(agent)
    if stewarded:
        raise HttpError(409, f"{agent.slug} stewards workspace project(s) {', '.join(stewarded)}; "
                             "hand each to another steward (PATCH …/workspace-projects/<ws>/<handle>/ "
                             "with steward) before deleting it")
    # Its sessions would otherwise outlive it: still listed, their emdash tasks
    # still on the box. Closed first, while they can still be told apart by agent.
    close_agent_sessions(agent)
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


@router.patch("/{slug}/repository", response=AgentDetailOut,
              summary="Point an agent at its GitHub repository")
def set_repository(request: HttpRequest, slug: str, payload: RepositoryIn) -> AgentDetailOut:
    """Set the repository (and optionally the branch) its runners clone and run —
    the one field the setup checklist's first step asks for, without the create
    wizard. A github.com URL in any spelling; stored as `https://github.com/owner/repo`.

    The agent's owner or an admin (`_agent_for_admin`): the repo is what a box
    runs as this agent, the same bar the upsert holds `DEFINITION_FIELDS` to. The
    cloud boxes that take its turns are asked to re-run their bootstrap, as a
    repo change through the upsert does."""
    from apps.events import audit

    from . import delegations
    from .setup import request_setup

    agent = _agent_for_admin(request, slug)
    full = delegations.repo_full_name(payload.repo_url.strip())
    if not full:
        raise HttpError(422, "give a github.com repository, e.g. https://github.com/dimagi/muse")
    url = f"https://github.com/{full}"
    ref = payload.repo_ref.strip() or agent.repo_ref
    before = {"repo_url": agent.repo_url, "repo_ref": agent.repo_ref}
    agent.repo_url, agent.repo_ref = url, ref
    agent.save(update_fields=["repo_url", "repo_ref", "updated_at"])
    audit.note_change(scope=f"agent:{agent.slug}/repository", before=before,
                      after={"repo_url": url, "repo_ref": ref})
    if before["repo_url"] != url:
        request_setup(agent)
    return _detail(request, agent)


@router.patch("/{slug}/credential-source", response=AgentDetailOut,
              summary="Set where an agent's secrets resolve from (1password | canopy-web)")
def set_credential_source(request: HttpRequest, slug: str,
                          payload: CredentialSourceIn) -> AgentDetailOut:
    """Choose the backend `canopy cred` resolves this agent's secrets from
    (canopy#850). `1password`: through `op`, with the agent's vault key.
    `canopy-web`: the AgentCredential values, via GET /credentials/resolve.

    The agent's owner or an admin only (`_agent_for_admin`), like the
    credentials and the vault pointer themselves: choosing where an agent's
    secrets come from is part of controlling them. Absent from AgentIn, so the
    repo's self-publish upsert cannot move it. From a browser it needs a fresh
    passkey step-up, like every other change to where secrets live."""
    agent = _agent_for_admin(request, slug)
    if stepup.is_browser(request):
        stepup.require_recent_step_up(request)
    if payload.credential_source == "canopy-web":
        # Zero-knowledge is the canopy-web vault's only mode (Jonathan,
        # 2026-10-17): moving here seals every stored value to the agent's
        # readers and wipes the custodial copies, or refuses when nobody could
        # read them yet.
        from . import zk

        try:
            services.make_zero_knowledge(agent)
        except zk.ZkError as exc:
            raise _zk_problem(exc) from exc
        agent.refresh_from_db()
    agent.credential_source = payload.credential_source
    agent.save(update_fields=["credential_source", "updated_at"])
    return _detail(request, agent)


@router.patch("/{slug}/device-binding", response=AgentDetailOut,
              summary="Require a runner device proof to resolve this agent's secrets")
def set_device_binding(request: HttpRequest, slug: str, payload: DeviceBindingIn) -> AgentDetailOut:
    """Canopy Vault (canopy-web#1538). When on and `credential_source` is
    `canopy-web`, `GET /credentials/resolve` answers 401
    `{"error": "device_proof_required"}` unless the request carries a DPoP proof
    from an ACTIVE runner device key on a runner this agent routes to, or one
    owned by the agent's owner. Owner or admin only, like the credential source."""
    agent = _agent_for_admin(request, slug)
    agent.require_device_binding = payload.require_device_binding
    agent.save(update_fields=["require_device_binding", "updated_at"])
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
        # offer "follow it instead". `runners` is the agent's own list, the same
        # rows `GET /runners` serves: leaving it empty here read as "routes
        # nowhere" to every caller of this endpoint (#1233).
        return AgentDefaultOrderOut(own=True, workspace=default_order_source(agent),
                                    runners=list_agent_runners(request, slug),
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
        directed=[r.name for r in inh.directed],
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
    from apps.harness.models import RunnerAssignment

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
    # Runners this list ADDS must be ones the caller administers; the ones it
    # keeps are taken as they are (`_runners_for_routing`).
    by_id = _runners_for_routing(
        request, agent, ids,
        RunnerAssignment.objects.filter(agent=agent, source="").values_list("runner_id", flat=True),
    )
    from apps.harness import routing_audit

    with transaction.atomic():
        before = routing_audit.agent_rows(agent, source="")
        # source="" ONLY. Source rules live in this table too, and an unscoped
        # delete here would destroy every one of them each time the default
        # order was saved.
        RunnerAssignment.objects.filter(agent=agent, source="").delete()
        RunnerAssignment.objects.bulk_create([
            RunnerAssignment(agent=agent, runner=by_id[row.runner_id], rank=i, enabled=row.enabled)
            for i, row in enumerate(rows_in)
        ])
        routing_audit.record_routing_change(
            request, workspace=agent.workspace,
            target=agent.slug, scope="default runner order",
            before=before, after=routing_audit.agent_rows(agent, source=""))
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
    from apps.harness.models import Runner, RunnerAssignment, Turn, agent_turns_q

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
        Turn.objects.filter(agent_turns_q(agent), status=Turn.QUEUED)
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


def _current_rules(agent) -> dict:
    """(source, actor) -> {"ids": runner ids in the rule, "turn_mode": its mode}."""
    from apps.harness.models import RunnerAssignment

    out: dict = {}
    for source, actor, runner_id, turn_mode in (
        RunnerAssignment.objects.filter(agent=agent).exclude(source="")
        .values_list("source", "actor", "runner_id", "turn_mode")
    ):
        rule = out.setdefault((source, actor), {"ids": set(), "turn_mode": turn_mode})
        rule["ids"].add(runner_id)
    return out


def _refuse_new_auto(request: HttpRequest, agent, source: str, actor: str, turn_mode: str,
                     current: dict) -> None:
    """Setting a rule to `auto` is for the agent's admins — but only SETTING it:
    keeping an `auto` an admin already set is not, or an editor could never save
    the rules around it."""
    if turn_mode == "auto" and current.get((source, actor), {}).get("turn_mode") != "auto":
        _refuse_auto_unless_admin(request, agent, f"a routing rule ({source}/{actor or 'anyone'})")


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
    from apps.harness.models import RunnerAssignment

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

    current = _current_rules(agent)

    # `auto` on a rule is the agent's admins' to set (docs/architecture/access.md).
    # Asked before the runners are, so an editor is told the actual reason.
    for r, actor in zip(payload.rules, actors):
        _refuse_new_auto(request, agent, r.source, actor, r.turn_mode, current)

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

    # Gated per rule on the runners each one ADDS (`_runners_for_routing`): a
    # runner already in that rule is kept as it is, so re-sending someone else's
    # rule unchanged is not a claim to administer their box.
    by_id: dict = {}
    for r, actor in zip(payload.rules, actors):
        by_id.update(_runners_for_routing(
            request, agent, [row.runner_id for row in r.runners],
            current.get((r.source, actor), {}).get("ids", ()),
        ))

    from apps.harness import routing_audit

    with transaction.atomic():
        before = routing_audit.agent_rows(agent, rules_only=True)
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
        routing_audit.record_routing_change(
            request, workspace=agent.workspace,
            target=agent.slug, scope="routing rules (all)",
            before=before, after=routing_audit.agent_rows(agent, rules_only=True))
    return list_agent_runner_rules(request, slug)


# ---- one rule at a time (#1143) ----
#
# A rule belongs to one (source, person) — the person's work, routed to boxes that
# may belong to someone else entirely. Saving ALL of an agent's rules to change
# one made every save depend on rights over every box any rule names, which no
# one holds once two people's boxes are involved. The Settings page saves here,
# one changed rule at a time; the wholesale PUT above stays for scripts.

def _rule_key_or_422(source: str, actor: str) -> tuple[str, str]:
    from apps.harness.models import Turn
    from apps.harness.schemas import RoutableSource
    from typing import get_args

    if source not in get_args(RoutableSource):
        raise HttpError(422, f"not a routable source: {source!r}")
    if not actor:
        return source, ""
    actor = _actor_or_422(actor)
    if source == Turn.ORIGIN_CANOPY_SCHEDULER:
        raise HttpError(422, "no person is behind canopy_scheduler turns, so a person's rule can't apply there")
    return source, actor


@router.put("/{slug}/runner-rules/{source}", response=list[AgentRunnerRuleOut],
            summary="Set one routing rule (a source, optionally one person's)")
def set_agent_runner_rule(
    request: HttpRequest, slug: str, source: str, payload: AgentRunnerRuleBodyIn, actor: str = "",
) -> list[AgentRunnerRuleOut]:
    """Route `source` turns — only `actor`'s, when given — to these runners, in
    preference order. Replaces that ONE rule and touches no other. Runners the
    rule already names are kept as they are; a runner it ADDS must be one you
    administer. `strict`: only these runners may take the work. Returns every rule."""
    from apps.harness.models import RunnerAssignment

    agent = _agent_for_write(request, slug)
    source, actor = _rule_key_or_422(source, actor)
    if not payload.runners:
        raise HttpError(422, "a rule needs at least one runner — DELETE removes a rule")
    ids = [row.runner_id for row in payload.runners]
    if len(ids) != len(set(ids)):
        raise HttpError(422, "a runner may appear once per rule")
    current = _current_rules(agent)
    _refuse_new_auto(request, agent, source, actor, payload.turn_mode, current)
    by_id = _runners_for_routing(request, agent, ids, current.get((source, actor), {}).get("ids", ()))

    from apps.harness import routing_audit

    scope = f"{source} rule" + (f" for {actor}" if actor else "")
    with transaction.atomic():
        before = routing_audit.agent_rows(agent, source=source, actor=actor)
        RunnerAssignment.objects.filter(agent=agent, source=source, actor=actor).delete()
        RunnerAssignment.objects.bulk_create([
            RunnerAssignment(
                agent=agent, runner=by_id[row.runner_id], rank=rank, source=source,
                actor=actor, strict=payload.strict, enabled=row.enabled,
                turn_mode=payload.turn_mode,
            )
            for rank, row in enumerate(payload.runners)
        ])
        routing_audit.record_routing_change(
            request, workspace=agent.workspace,
            target=agent.slug, scope=scope, before=before,
            after=routing_audit.agent_rows(agent, source=source, actor=actor))
    return list_agent_runner_rules(request, slug)


@router.delete("/{slug}/runner-rules/{source}", response={204: None},
               summary="Remove one routing rule")
def delete_agent_runner_rule(request: HttpRequest, slug: str, source: str, actor: str = ""):
    """Remove the rule for `source` (and `actor`, when given). That work goes back
    to the next rule down and the agent's default order. Idempotent."""
    from apps.harness.models import RunnerAssignment

    from apps.harness import routing_audit

    agent = _agent_for_write(request, slug)
    source, actor = _rule_key_or_422(source, actor)
    with transaction.atomic():
        before = routing_audit.agent_rows(agent, source=source, actor=actor)
        RunnerAssignment.objects.filter(agent=agent, source=source, actor=actor).delete()
        routing_audit.record_routing_change(
            request, workspace=agent.workspace,
            target=agent.slug, scope=f"{source} rule" + (f" for {actor}" if actor else ""),
            before=before, after=[])
    return Status(204, None)


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
    from apps.harness.models import RunnerAssignment

    agent = _agent_for_write(request, slug)
    actor = _actor_or_422(actor)
    current = _current_rules(agent)

    sources = list(dict.fromkeys(payload.sources or _actor_sources()))
    dead = [s for s in sources if s not in _actor_sources()]
    if dead:
        raise HttpError(422, f"no person is behind {', '.join(dead)} turns, so a person's route can't apply there")
    if not payload.runners:
        raise HttpError(422, "a route needs at least one runner — DELETE removes a route")
    ids = [row.runner_id for row in payload.runners]
    if len(ids) != len(set(ids)):
        raise HttpError(422, "a runner may appear once per route")

    # Per source, like the rules it writes: `auto` and the runners are gated on
    # what changes in each (`_refuse_new_auto`, `_runners_for_routing`).
    runners: dict = {}
    for source in sources:
        _refuse_new_auto(request, agent, source, actor, payload.turn_mode, current)
        runners.update(_runners_for_routing(
            request, agent, ids, current.get((source, actor), {}).get("ids", ())))

    from apps.harness import routing_audit

    with transaction.atomic():
        before = routing_audit.agent_rows(agent, actor=actor, rules_only=True)
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
        routing_audit.record_routing_change(
            request, workspace=agent.workspace,
            target=agent.slug, scope=f"route for {actor}", before=before,
            after=routing_audit.agent_rows(agent, actor=actor, rules_only=True))
    return [r for r in list_agent_actor_routes(request, slug) if r.actor == actor]


@router.delete("/{slug}/actor-routes/{actor}", response={204: None},
               summary="Remove one person's routing")
def delete_agent_actor_route(request: HttpRequest, slug: str, actor: str):
    """Stop routing this person's work specially: their turns go back to the
    agent's source rules and default runner order. Touches no one else's rules.
    Idempotent — removing a person with no route is a 204."""
    from apps.harness.models import RunnerAssignment

    from apps.harness import routing_audit

    agent = _agent_for_write(request, slug)
    actor = _actor_or_422(actor)
    with transaction.atomic():
        before = routing_audit.agent_rows(agent, actor=actor, rules_only=True)
        RunnerAssignment.objects.filter(agent=agent, actor=actor).exclude(source="").delete()
        routing_audit.record_routing_change(
            request, workspace=agent.workspace,
            target=agent.slug, scope=f"route for {actor}", before=before, after=[])
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
    services.stamp_share_urls(turns)
    items = [AgentTurnOut.model_validate(t) for t in turns]
    return paginate(items, offset=0, limit=limit)


@router.post("/{slug}/turns/", response={201: AgentTurnOut},
             summary="Package a turn (idempotent per cli_session_id)",)
def create_turn(request: HttpRequest, slug: str, payload: AgentTurnIn) -> Status:
    agent = _agent_for_write(request, slug)
    from apps.harness import initiator as who

    # A report-only row (no dispatch matched) is a turn someone ran by hand; the
    # caller posting its close-out is the best account of who that was.
    turn = services.upsert_turn(agent, payload,
                                initiator=who.for_request(request, via="closeout"))
    services.stamp_share_urls([turn])
    return Status(201, AgentTurnOut.model_validate(turn))


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
        (project._task_count, project._open_task_count,
         project._waiting_task_count) = counts.get(project.pk, (0, 0, 0))
    return [AgentProjectOut.model_validate(p) for p in projects]


@router.post("/{slug}/projects/", response={201: AgentProjectOut}, summary="Create a project",)
def create_project(request: HttpRequest, slug: str, payload: AgentProjectIn) -> Status:
    agent = _agent_for_write(request, slug)
    try:
        project = services.create_project(agent, payload)
    except services.UnknownPersonError as exc:
        raise HttpError(422, str(exc)) from exc
    return Status(201, AgentProjectOut.model_validate(project))


#: How many of the agent's turns a project page shows.
_PROJECT_RECENT_TURNS = 10


def _turns_touching(agent, ext_ids: list[str]) -> list:
    """The agent's latest turns whose `task_ext_ids` name any of `ext_ids`.

    `task_ext_ids` is a JSON list. Postgres answers "contains this element"
    natively; SQLite (the test database) has no such lookup, so there the
    element is matched as its quoted JSON text — exact, since `"T1"` cannot
    match inside `"T12"`.
    """
    from django.db import connection

    from apps.harness.models import Turn, agent_turns_q

    if not ext_ids:
        return []
    q = Q()
    for eid in ext_ids:
        if connection.features.supports_json_field_contains:
            q |= Q(task_ext_ids__contains=[eid])
        else:
            q |= Q(task_ext_ids__icontains=f'"{eid}"')
    # `redact` reads each turn's agent and claiming box; join them up front.
    return list(Turn.objects.filter(agent_turns_q(agent)).filter(q)
                .select_related("agent", "claimed_by")
                .order_by("-created_at")[:_PROJECT_RECENT_TURNS])


def _turn_brief(turn) -> TurnBriefOut:
    preview = (turn.report_title or turn.prompt or "").strip()
    return TurnBriefOut(id=turn.id, status=turn.status, prompt_preview=preview[:200],
                        created_at=turn.created_at, task_ext_ids=list(turn.task_ext_ids or []))


@router.get("/{slug}/projects/{ref}/", response=AgentProjectDetailOut,
            summary="Get one project: its fields and links, its tasks, its recent turns",)
def get_project(request: HttpRequest, slug: str, ref: str) -> AgentProjectDetailOut:
    agent = _get_agent_or_404(request, slug)
    return project_detail(request, _get_project_or_404(agent, ref))


def project_detail(request: HttpRequest, project) -> AgentProjectDetailOut:
    """One project's page — shared by the agent route and the workspace-project route."""
    from apps.harness import turn_access

    live_first = Case(When(status__in=services.LIVE_STATUSES, then=0), default=1,
                      output_field=IntegerField())
    tasks = list(project.tasks.select_related("agent", "project", "waiting_on_user", "parent")
                 .prefetch_related("human_whys")
                 .order_by(live_first, "position", "id"))
    project._task_count = len(tasks)
    project._open_task_count = sum(t.status in services.LIVE_STATUSES for t in tasks)
    # `waiting_task_count` is left to the model property: one COUNT through
    # `services.waiting_q`, rather than a second Python copy of that predicate.
    # Everyone sees what the agent did; a turn's prompt is a log (turn_access).
    # ext_ids are per agent, so a workspace project's turns are found per task owner.
    by_agent: dict = {}
    for t in tasks:
        by_agent.setdefault(t.agent_id, (t.agent, []))[1].append(t.ext_id)
    found = [turn for a, eids in by_agent.values() for turn in _turns_touching(a, eids)]
    found = sorted({turn.pk: turn for turn in found}.values(), key=lambda turn: turn.created_at,
                   reverse=True)[:_PROJECT_RECENT_TURNS]
    turns = turn_access.redact(found, request.user)
    # Built from the plain project shape: `project.tasks` is a manager, not a list.
    from . import participants

    from . import proposals

    return AgentProjectDetailOut(
        **AgentProjectOut.model_validate(project).model_dump(),
        tasks=[AgentTaskOut.model_validate(t) for t in tasks],
        recent_turns=[_turn_brief(t) for t in turns],
        # Same gate as the rest of the project: members of the agent's workspace.
        participants=[ProjectParticipantOut(**p) for p in participants.of_project(project)],
        proposal=(proposals.card(project, request.user) if proposals.is_proposal(project) else None),
        proposed_tasks=[ProposedTaskOut(**proposals.task_card(t, request.user))
                        for t in proposals.proposed_tasks_of(project).filter(status="suggested")],
        board_readable=project.agent.workspace_id in _visible_agent_workspace_ids(request),
    )


@router.patch("/{slug}/projects/{ref}/", response=AgentProjectOut, summary="Update a project",)
def patch_project(request: HttpRequest, slug: str, ref: str,
                  payload: AgentProjectPatch) -> AgentProjectOut:
    agent = _agent_for_write(request, slug)
    project = _get_project_or_404(agent, ref)
    data = payload.model_dump(exclude_unset=True)
    if "links" in data and data["links"] is not None:
        data["links"] = [link if isinstance(link, dict) else link.model_dump()
                         for link in data["links"]]
    return AgentProjectOut.model_validate(apply_project_patch(request, project, data))


def _require_workspace_editor(request: HttpRequest, workspace: str, steward) -> None:
    """Placing a project in a workspace other than its steward's makes it that
    workspace's, so the caller must be able to edit there too — writing the
    steward is not enough. Same 404 as a missing workspace for a non-member."""
    if not workspace or workspace == steward.workspace_id:
        return
    if not perms.can(request.user, workspace, perms.CONTENT_WRITE):
        raise HttpError(404, f"workspace '{workspace}' not found")


def apply_project_patch(request: HttpRequest, project, data: dict):
    """A PATCH body against one project, from either address. `handle` promotes
    it to a workspace project; `steward` hands a workspace project on — and the
    caller must be able to write the new steward too."""
    if "spawned_from" in data:
        from .proposals_api import resolve_task_ref

        ref_ = (data.pop("spawned_from") or "").strip()
        project.spawned_from = resolve_task_ref(request, ref_) if ref_ else None
    handle, steward = data.pop("handle", None), data.pop("steward", None)
    workspace = (data.pop("workspace", None) or "").strip()
    if workspace and handle is None:
        raise HttpError(422, "workspace is set with handle, when the project is promoted")
    _require_workspace_editor(request, workspace, project.agent)
    try:
        with transaction.atomic():
            if handle is not None:
                project = services.promote_project(project, handle, workspace)
            if steward is not None:
                project = services.set_steward(project, _agent_for_write(request, steward.strip()))
            project = services.patch_project(project, data)
    except services.UnknownPersonError as exc:
        raise HttpError(422, str(exc)) from exc
    except services.ProjectScopeError as exc:
        raise HttpError(exc.status, str(exc)) from exc
    return project


# ---- tasks, addressed by `ext_id` ----


def _get_task_or_404(agent, ref: str):
    task = services.get_task(agent, ref)
    if task is None:
        raise HttpError(404, f"task {ref} not found")
    return task


def _is_agent_itself(request: HttpRequest, agent) -> bool:
    return agent.user_id is not None and agent.user_id == request.user.pk


@router.get("/{slug}/tasks/", response=list[AgentTaskOut], summary="List the agent's tasks",)
def list_tasks(request: HttpRequest, slug: str, project: str = "", status: str = "",
               waiting: str = "", ask: str = "", batch: str = "",
               parent: str = "") -> list[AgentTaskOut]:
    """Filters: `project` (an ext_id, or `none` for one-offs), `status`
    (comma-separated), `waiting=me` (open asks nobody owns plus tasks parked on
    you), `ask=open|closed`, `batch` (a batch_key), `parent` (an ext_id for
    that task's direct subtasks, or `none` for top-level tasks only)."""
    agent = _get_agent_or_404(request, slug)
    qs = services.filter_tasks(
        agent.tasks.select_related("agent", "project", "waiting_on_user", "parent")
        .prefetch_related("human_whys"),
        user=request.user, project=project, status=status, waiting=waiting, ask=ask, batch=batch,
        parent=parent)
    return [AgentTaskOut.model_validate(t) for t in qs]


@router.post("/{slug}/tasks/", response={201: list[AgentTaskOut]},
             summary="Create tasks (a list; an idempotency_key replays instead of duplicating)",)
def create_tasks(request: HttpRequest, slug: str, payload: list[AgentTaskIn]) -> Status:
    agent = _get_agent_or_404(request, slug)
    # A task can carry `on_approve` — work that runs the moment a viewer approves
    # it — so WRITING one is the reshaping tier: an editor, or the agent itself
    # under its own login.
    if not _is_agent_itself(request, agent):
        agent = _agent_for_write(request, slug)
    try:
        tasks = services.create_tasks(agent, [p.model_dump(exclude_unset=True) for p in payload])
    except services.UnknownPersonError as exc:
        raise HttpError(422, str(exc)) from exc
    except services.DuplicateTaskError as exc:
        # An explicit `ext_id` the agent already uses, with no idempotency key
        # to say "this is the same task".
        raise HttpError(409, f"{exc} — omit ext_id, or send an idempotency_key to replay") from exc
    except ValueError as exc:
        raise HttpError(422, str(exc)) from exc
    return Status(201, [AgentTaskOut.model_validate(t) for t in tasks])


@router.get("/{slug}/tasks/{ref}/", response=AgentTaskDetailOut,
            summary="Get one task with everything done to it",)
def get_task(request: HttpRequest, slug: str, ref: str) -> AgentTaskDetailOut:
    agent = _get_agent_or_404(request, slug)
    task = _get_task_or_404(agent, ref)
    # Built from the plain task shape: `task.actions` is a manager, not a list.
    return AgentTaskDetailOut(
        **AgentTaskOut.model_validate(task).model_dump(),
        actions=[AgentTaskActionOut.model_validate(a)
                 for a in task.actions.select_related("agent", "task")],
    )


@router.patch("/{slug}/tasks/{ref}/", response=AgentTaskOut, summary="Update a task's fields",)
def patch_task(request: HttpRequest, slug: str, ref: str, payload: AgentTaskPatch) -> AgentTaskOut:
    agent = _agent_for_write(request, slug)
    task = _get_task_or_404(agent, ref)
    data = payload.model_dump(exclude_unset=True)
    try:
        return AgentTaskOut.model_validate(services.patch_task(task, data))
    except (services.UnknownPersonError, ValueError) as exc:
        # TaskTreeError, and a quote whose person_id is not known here.
        raise HttpError(422, str(exc)) from exc


#: Actions that RESHAPE work rather than answer it: `nudge` starts an agent turn
#: on work already under way (the QuickTurn tier) and `done` declares it
#: finished. approve / decline / reply answer what is already on the board — the
#: viewer tier ("decide, read the board").
_EDITOR_ACTIONS = frozenset({"nudge", "done"})

#: Task actions on a project's task that count as a person engaging with the project.
_ENGAGEMENT_OF_ACTION = {"approve": "approved", "decline": "declined", "reply": "commented"}


def _engaged_with_project_task(request: HttpRequest, task, action: str) -> None:
    """Record what a person just did to one of a project's tasks (`EngagementEvent`),
    and make a person who replied a participant of the project. Never fails the
    action: both writes are best-effort."""
    from apps.contacts import services as contacts

    from . import engagement, participants

    kind = _ENGAGEMENT_OF_ACTION.get(action)
    if kind is None:
        return
    engagement.record(kind, workspace=task.agent.workspace_id, surface=engagement.surface_of(request),
                      user=request.user, project=task.project, task=task)
    if action == "reply":
        try:
            person = contacts.person_for(user=request.user)
            participants.ensure(task.project, person, source="reply")
        except Exception:  # noqa: BLE001 — a participant is never worth a failed reply
            logger.exception("could not add %s as a participant of %s", request.user.pk, task.project_id)


@router.post("/{slug}/tasks/{ref}/actions", response=ActOut,
             summary="Act on a task: approve, decline, reply, nudge or done",)
def act_on_task(request: HttpRequest, slug: str, ref: str, payload: AgentTaskActionIn) -> ActOut:
    """approve → in progress, and the agent starts now: its `on_approve` turns,
    or one turn written from the card when it has none. decline → declined (the
    comment is the reason). reply → on an open question, the answer (closes the
    ask; runs `on_approve`, or with none starts one turn carrying the answer —
    whoever answers, unless it is the agent itself); on anything else a note —
    an editor's note starts a turn carrying it, a viewer's waits for the
    agent's next turn. nudge (editor, in-progress tasks) → start a turn on it
    now, status unchanged. done → done (editor). 409 when the task is not in a
    state the action applies to (an ask already closed, a finished task,
    nudging one that is not in progress); `turn_ids` names the turns the action
    started."""
    agent = _get_agent_or_404(request, slug)
    task = _get_task_or_404(agent, ref)
    if payload.action in _EDITOR_ACTIONS:
        from . import ideas

        # The person whose idea it is may call it shipped, as its owner may (hal T82).
        if not (payload.action == "done" and ideas.is_ideas_person(task, request.user)):
            _agent_for_write(request, slug)
    try:
        task, row, turns = services.act(
            task, action=payload.action, comment=payload.comment,
            by=request.user.email or request.user.get_username(), by_user=request.user,
            actor_workspace_ids=_visible_agent_workspace_ids(request),
            # Who may start a turn is the QuickTurn gate (`enqueue_turn` needs
            # write); it decides whether a reply wakes the agent. Never the agent
            # replying on its own card: its note would wake itself, and a turn
            # that replies again would wake it again.
            may_start_turns=(_can(request, agent.workspace_id, perms.AGENT_WORK)
                             and not _is_agent_itself(request, agent)),
            # An ANSWER to an open question wakes the agent whoever writes it (it
            # is the response the agent asked for) — but never the agent itself.
            by_agent_itself=_is_agent_itself(request, agent))
    except services.ClosedAskError as exc:
        raise HttpError(409, str(exc)) from exc
    except PermissionError as exc:
        # A proposed project's decision card, by someone who may not decide it.
        raise HttpError(403, str(exc)) from exc
    except ValueError as exc:
        # An empty reply, or an `on_approve` spec that cannot run. `act` is
        # atomic: the ask is still open and the action can be retried.
        raise HttpError(422, str(exc)) from exc
    if task.project_id is not None and not _is_agent_itself(request, agent):
        _engaged_with_project_task(request, task, payload.action)
    return ActOut(task=AgentTaskOut.model_validate(task),
                  action=AgentTaskActionOut.model_validate(row),
                  turn_ids=[t.id for t in turns])


# ---- the agent's queue: actions it still has to carry out ----


@router.get("/{slug}/actions/", response=list[AgentTaskActionOut],
            summary="List actions on the agent's tasks (the agent drains ?status=pending)",)
def list_task_actions(request: HttpRequest, slug: str, status: str = "",
                      limit: int = 200) -> list[AgentTaskActionOut]:
    """At most `limit` rows (default 200, cap 500). Unfiltered, PENDING rows come
    first (newest first), then the rest newest first — so a short page still
    carries the agent's whole queue before any history."""
    agent = _get_agent_or_404(request, slug)
    limit = clamp_limit(limit)
    if status == "pending":
        rows = services.pending_actions(agent)  # oldest first: the order to carry them out
    else:
        rows = agent.task_actions.select_related("agent", "task")
        if status:
            rows = rows.filter(status=status).order_by("-created_at", "-id")
        else:
            pending_first = Case(When(status="pending", then=0), default=1,
                                 output_field=IntegerField())
            rows = rows.order_by(pending_first, "-created_at", "-id")
    return [AgentTaskActionOut.model_validate(a) for a in rows[:limit]]


@router.post("/{slug}/actions/{action_id}/applied", response=AgentTaskActionOut,
             summary="Mark an action applied (the agent calls this after carrying it out)",)
def mark_task_action_applied(request: HttpRequest, slug: str, action_id: int,
                             payload: ActionAppliedIn) -> AgentTaskActionOut:
    # Marking an action applied tells the agent it is done, so the agent never
    # carries it out — the agent's own login or the reshaping tier, not any viewer.
    agent = _get_agent_or_404(request, slug)
    if not _is_agent_itself(request, agent):
        agent = _agent_for_write(request, slug)
    row = agent.task_actions.filter(id=action_id).select_related("agent", "task").first()
    if row is None:
        raise HttpError(404, f"action {action_id} not found")
    return AgentTaskActionOut.model_validate(services.mark_applied(row, payload.result_note))


# ---- Agent credentials (spec 2026-09-05-agent-credentials-design) -----------

@router.put("/{slug}/credentials", response=list[AgentCredentialStatusOut],
            summary="Set named secrets for an agent (write-only)")
def set_agent_credentials(request: HttpRequest, slug: str, payload: AgentCredentialsIn):
    """Upsert. Non-clobbering: a ref absent from the body is untouched.

    The response is the MASKED status. From a browser session this needs a
    fresh passkey step-up (403 `step_up_required` otherwise); a token write —
    `canopy vault import` — does not, and is recorded like every settings change."""
    agent = _agent_for_admin(request, slug)
    # Step-up only for a person at a browser (canopy-web#1538): a stolen session
    # cookie is the threat it closes. A PAT write has no browser to ask and is
    # audited as a SettingChange like any other; requiring a passkey there would
    # simply break `canopy vault import`.
    if stepup.is_browser(request):
        stepup.require_recent_step_up(request)
    from .zk import ZkError

    try:
        services.set_agent_credentials(agent, payload.values, user=request.user)
    except ZkError as exc:  # zero-knowledge, but nobody to seal to yet
        raise _zk_problem(exc) from exc
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


def _mailbox_backend(agent) -> str:
    """"ses" when the agent's address is canopy-hosted (nothing to sign in; `canopy email`
    goes through canopy-web), "google" for any other address, "" with none."""
    from . import mailbox

    kind = mailbox.describe(agent)["kind"]
    return {"hosted": "ses", "google": "google"}.get(kind, "")


def _credential_access(request: HttpRequest, agent) -> tuple[str | None, str]:
    """Who the caller is, for the purpose of reading this agent's plaintext:
    `("runner", "")`, `("admin", "")`, or `(None, <what access to get>)`.

    The one decision both `/credentials/resolve` and `/credentials/access`
    make, so the cheap check can never disagree with the real gate. `runner`
    wins when both hold (`caller_runs_agent` already implies admin), because it
    is the more specific claim and the audit trail should say a box read it.

    A runner token (`RunnerAccessToken`, bought with a device key) signs in as
    the runner's owner, but it speaks for ONE box: it is a `runner` caller only
    for an agent that box serves (`device_keys.runner_serves_agent`), and never
    an `admin` — the owner's other agents stay out of a box's reach.
    """
    rtok = getattr(request, "runner_token", None)
    if rtok is not None:
        from apps.harness.device_keys import runner_serves_agent

        if agent.is_admin(request.user) and runner_serves_agent(rtok.runner, agent):
            return "runner", ""
        return None, (f"this runner token is {rtok.runner.name}'s, and {rtok.runner.name} "
                      f"does not run {agent.slug}")
    if services.caller_runs_agent(request.user, agent):
        return "runner", ""
    if agent.is_admin(request.user):
        return "admin", ""
    owner = getattr(agent.owner, "email", "") if agent.owner_id else ""
    ask = f"ask its owner ({owner})" if owner else "ask its owner or a workspace owner"
    return None, (
        f"resolving {agent.slug}'s secrets needs either a live runner you pair that "
        f"{agent.slug} routes to, or being {agent.slug}'s owner or an agent admin; "
        f"{ask} to make you an admin (PUT /api/agents/{agent.slug}/admins/<your user id>)"
    )


@router.get("/{slug}/credentials/access", response=AgentCredentialAccessOut,
            summary="May the caller resolve this agent's secrets? (never values)")
def agent_credential_access(request: HttpRequest, slug: str) -> AgentCredentialAccessOut:
    """The cheap check `canopy cred` makes before fetching anything
    (canopy#850): which backend the agent resolves from, and whether THIS
    caller would be let through `/credentials/resolve` — and as whom.

    Session or bearer: it returns no values, so the browser may ask it too.
    A non-member gets 404, as everywhere else. `may_resolve` does not account
    for resolve's bearer-only rule, which is a property of the request rather
    than the caller."""
    agent = _get_agent_or_404(request, slug)
    via, reason = _credential_access(request, agent)
    return AgentCredentialAccessOut(
        agent=agent.slug, credential_source=agent.credential_source,
        may_resolve=via is not None, via=via, reason=reason,
    )


@router.get("/{slug}/credentials/resolve",
            response={200: AgentCredentialsResolveOut, 401: DeviceProofRequiredOut,
                      409: ZkWrapMissingOut},
            summary="PLAINTEXT — a runner stages this agent's secrets")
def resolve_agent_credentials(request: HttpRequest, slug: str, refs: str | None = None):
    """The one route that returns values, and it is not for a browser.

    **Scoping (`?refs=a,b`).** Without `refs` the response is everything, as it
    always was. With `refs`, `values` holds only the named refs that the
    agent's runtime.yaml also declares, and the secret extras
    (`op_sa_token`, `shared_op_sa_token`, `github_token`, `salesforce_creds`)
    are included only when named in `refs` too. `op_vault`, `shared_op_vault`
    and `mailbox` are not secrets and are always returned.

    **`credential_source == "canopy-web"`** leaves out the agent's 1Password
    service-account token (`op_sa_token` is ""): its values come from here, so
    the box has no reason to hold a key to its vault.

    **Device proof.** With `require_device_binding` on and `credential_source`
    `canopy-web`, the request must also carry a `DPoP` header: an RFC 9449
    proof (`typ: dpop+jwt`, `alg: ES256`, public `jwk` in the header; claims
    `htm`, `htu` without query, `iat` within ±120 s, single-use `jti`) signed by
    an ACTIVE runner device key on a runner this agent routes to or one its
    owner owns. Otherwise: 401 `{"error": "device_proof_required"}`.
    An EC2 cloud runner proves its instance role instead, in
    `X-Canopy-Aws-Proof`: base64 JSON `{method, url, headers, body}` of a
    SigV4-signed, unsent `sts:GetCallerIdentity` for `https://sts.amazonaws.com/`
    whose signed headers include `X-Canopy-Proof-Htu` (this URL) and
    `X-Canopy-Proof-Iat` (±120 s); single-use. canopy-web sends it to the pinned
    STS endpoint and accepts the returned role if an ACTIVE `aws-iam` device key
    for it sits on a runner this agent routes to.

    **Alerts.** With `credential_source` `canopy-web`, the agent's owner is told
    (email + push, at most once per kind per hour) of a box read (`via`
    runner) with no `X-Canopy-Turn-Id`, a read of an undeclared ref, a burst of
    reads, or a refusal for a bad or missing device proof.

    **Read log.** Every ref handed out is recorded with the caller, the runner
    when known (the proving device key's runner, else `X-Canopy-Runner: <runner
    id>`, honoured only for a runner the caller owns) and `X-Canopy-Turn-Id`.

    Two gates, both required:

    1. **Bearer only.** A session cookie is refused even for the owner. That is
       what makes "the browser never sees plaintext" a property of the system
       rather than a habit of the UI — a future page cannot accidentally acquire
       the ability to render a secret.
    2. **The caller must be one of two principals** (`_credential_access`):
       - `runner`: pairs a live runner this agent routes to. Mirrors the runner
         credential fetch, whose boundary is "the caller who can claim turns as
         this runner".
       - `admin`: is the agent's owner or an admin (`Agent.is_admin`) — a human
         resolving for a local Claude Code session (canopy#850). Not a widening
         in substance: an admin can already write every value, swap the vault
         key, and put their own box on the agent to read them back.
       Anyone else — a member, an editor — is refused, and told which access to
       ask for. Tighter than workspace membership on purpose.

    Every read is recorded, so a credential fetch is visible in the fleet log
    rather than silent.
    """
    if not request.META.get("HTTP_AUTHORIZATION", "").startswith("Bearer "):
        raise HttpError(403, "resolve requires a bearer token; a browser session is never given values")

    agent = _get_agent_or_404(request, slug)
    via, reason = _credential_access(request, agent)
    if via is None:
        raise HttpError(403, reason)

    # Device proof (canopy-web#1538 Phase 1a). Verified whenever one is sent, so
    # the read log can name the box that proved itself; REQUIRED only when the
    # agent asks for it and resolves from canopy-web.
    device_key, refused, unrouted = _device_key_from_proof(request, agent)
    # A runner token speaks for one box: a proof from some other box's key does
    # not go with it.
    rtok = getattr(request, "runner_token", None)
    if rtok is not None and device_key is not None and device_key.runner_id != rtok.runner_id:
        raise HttpError(403, f"the device proof is {device_key.runner.name}'s, not this "
                             f"runner token's ({rtok.runner.name})")
    # A zero-knowledge agent ALWAYS needs one (canopy-web#1550): the wrap handed
    # out is the proving device's, so there is nothing to give without it.
    if (device_key is None
            and (agent.zk_enabled
                 or (agent.require_device_binding and agent.credential_source == agent.CANOPY_WEB))):
        from . import vault_alerts

        if unrouted is not None:
            # An APPROVED box that does not run this agent: a routing fact, not a
            # misused token, so the owner is not emailed (fizzy, canopy-web#1563).
            vault_alerts.unrouted_box_refused(agent, request, runner=unrouted)
            return JsonResponse({"error": "device_proof_required", "reason": "not_routed",
                                 "detail": refused}, status=401,
                                headers={"WWW-Authenticate": 'DPoP algs="ES256"'})
        vault_alerts.device_proof_refused(agent, request, reason=refused or "no device proof")
        return JsonResponse({"error": "device_proof_required", "reason": "no_valid_proof",
                             "detail": refused or "no device proof"}, status=401,
                            headers={"WWW-Authenticate": 'DPoP algs="ES256"'})

    wanted = _parse_refs(refs)
    zk_wrap = None
    if agent.zk_enabled:
        from . import zk

        values, zk_wrap = zk.resolve_payload(agent, device_key, wanted)
        # `zks1` values (server-minted, sealed to each device) open with the
        # device's own key; only `zk1` values need its vault-key wrap.
        if zk_wrap is None and zk.needs_wrap(values):
            zk.device_wrap_missing(agent, request, device_key)
            return JsonResponse({"error": "zk_wrap_missing", "kv": agent.zk_kv,
                                 "device_key": str(device_key.pk)}, status=409)
    else:
        values = services.resolve_agent_credentials(agent, wanted)
    vault, op_token = services.resolve_agent_vault(agent)
    if agent.credential_source == agent.CANOPY_WEB:
        op_token = ""
    shared_vault, shared_token = services.resolve_shared_vault(agent)

    def extra(name: str) -> bool:
        return wanted is None or name in wanted

    delegation = delegations.delegation_for(agent) if extra("github_token") else None
    out = AgentCredentialsResolveOut(
        values=values, op_vault=vault,
        op_sa_token=op_token if extra("op_sa_token") else "",
        shared_op_vault=shared_vault,
        shared_op_sa_token=shared_token if extra("shared_op_sa_token") else "",
        github_token=delegations.decrypt_secret(delegation.secret_enc) if delegation else "",
        salesforce_creds=delegations.salesforce_creds_for(agent) if extra("salesforce_creds") else "",
        mailbox=agent.email or "",
        mailbox_backend=_mailbox_backend(agent),
        google_account=agent.google_identity(),
        zk=agent.zk_enabled, kv=agent.zk_kv if agent.zk_enabled else None, wrap=zk_wrap,
        agent_id=str(agent.pk) if agent.zk_enabled else "",
    )
    read = list(values) + [n for n in RESOLVE_SECRET_EXTRAS if getattr(out, n)]
    runner = device_key.runner if device_key is not None else _caller_runner(request)
    # A read is a BOX's only when this request names one: a device proof, or an
    # `X-Canopy-Runner` the caller owns. Pairing a runner is a fact about the
    # caller, not the request, so an owner who also pairs a box and resolves by
    # hand from their own session is an admin read, not a box outside a turn
    # (canopy-web#1563). Every `runner` caller is an admin already.
    if via == "runner" and runner is None:
        via = "admin"
    # The owner at their OWN laptop: their own token (not the box's runner token),
    # proved by that laptop's approved device key. A zero-knowledge agent always
    # needs the proof, so without this every emdash session the owner opened read
    # as "a box outside a turn" and emailed them about themselves (2026-10-18).
    # The read log still names the laptop. A cloud box, a bare X-Canopy-Runner, a
    # runner token, or someone else's laptop is still the box reading.
    if (via == "runner" and rtok is None and device_key is not None
            and runner.kind == runner.EMDASH and runner.owner_id == request.user.pk):
        via = "admin"
    turn_id = request.META.get("HTTP_X_CANOPY_TURN_ID", "")[:120]
    try:
        services.record_credential_reads(
            agent, read, user=request.user, runner=runner, via=via, turn_id=turn_id)
    except Exception:  # noqa: BLE001 - a log hiccup must not deny a runner its secrets
        logger.exception("credential read log failed for %s", agent.slug)
    from . import vault_alerts

    # Never raises (canopy-web#1549): an alert hiccup must not deny a runner either.
    vault_alerts.after_read(agent, request, via=via, turn_id=turn_id, asked=wanted,
                            handed=list(values), runner=runner)
    try:
        from apps.events import services as events

        events.record(
            [{
                "source": "agents.credentials",
                "kind": "agent.credentials.resolved",
                "level": "info",
                "key": f"{agent.slug}:{request.user.pk}",
                "summary": f"{len(values)} secret(s) resolved for {agent.slug}",
                "payload": {"agent": agent.slug, "count": len(values), "via": via,
                            "refs": sorted(read), "scoped": wanted is not None,
                            "runner": str(runner.pk) if runner else "",
                            "turn_id": turn_id},
            }],
            workspace=agent.workspace,
        )
    except Exception:  # noqa: BLE001 - an audit hiccup must not deny a runner its secrets
        pass
    return out


#: The secret fields of AgentCredentialsResolveOut beside `values`. A scoped
#: resolve (`?refs=`) returns each only when it is named; the read log names
#: each one handed out, like a ref.
RESOLVE_SECRET_EXTRAS = ("op_sa_token", "shared_op_sa_token", "github_token", "salesforce_creds")


def _parse_refs(refs: str | None) -> set[str] | None:
    """`?refs=a,b` → {"a", "b"}; absent → None (unscoped). Present but empty
    (`?refs=`) is a scope of nothing, never "everything"."""
    if refs is None:
        return None
    return {r.strip() for r in refs.split(",") if r.strip()}


def _device_key_from_proof(request: HttpRequest, agent):
    """`(key, refused, unrouted)`: the ACTIVE RunnerDeviceKey whose proof this
    request carries, if the proof verifies and its runner serves `agent`; else
    None and why. `unrouted` is the runner when the proof is GOOD — an approved,
    active key — and only the routing says no: the box simply does not run this
    agent (`device_keys.runner_serves_agent`). That is a different answer from a
    proof nobody approved, and the two get different follow-ups. A bad proof is
    not an error here — whether one is REQUIRED is the caller's decision.

    Two proofs: a DPoP proof from a key the box holds (`DPoP`, Phase 1a), or an
    EC2 box's instance role (`X-Canopy-Aws-Proof`, Phase 1b — a signed
    sts:GetCallerIdentity canopy-web replays to the pinned STS endpoint;
    `apps/harness/aws_iam_proof.py`)."""
    from apps.harness import aws_iam_proof, device_keys

    aws = request.META.get(aws_iam_proof.PROOF_HEADER, "")
    if aws:
        try:
            role = aws_iam_proof.verify(aws, request)
        except aws_iam_proof.AwsProofError as exc:
            logger.info("aws iam proof refused for %s: %s", agent.slug, exc)
            return None, f"AWS IAM proof refused: {exc}", None
        key = aws_iam_proof.active_key_for_agent(agent, role)
        if key is not None:
            return key, None, None
        known = aws_iam_proof.known_key(role)
        if known is not None:
            return None, f"{known.runner.name} does not run {agent.slug}", known.runner
        return None, f"no approved device key for {role}", None
    if not request.META.get("HTTP_DPOP"):
        return None, "no device proof", None
    try:
        jkt = device_keys.verify_proof(request)
    except device_keys.ProofError as exc:
        logger.info("device proof refused for %s: %s", agent.slug, exc)
        return None, f"DPoP proof refused: {exc}", None
    key = device_keys.active_key_for_agent(agent, jkt)
    if key is not None:
        return key, None, None
    known = device_keys.known_key(jkt)
    if known is not None:
        return None, f"{known.runner.name} does not run {agent.slug}", known.runner
    return None, "the DPoP key is not an approved device key", None


def _caller_runner(request: HttpRequest):
    """The runner named by `X-Canopy-Runner`, only when the caller owns it —
    a header naming someone else's box is ignored rather than recorded."""
    import uuid as _uuid

    raw = request.META.get("HTTP_X_CANOPY_RUNNER", "").strip()
    if not raw:
        return None
    try:
        rid = _uuid.UUID(raw)
    except ValueError:
        return None
    from apps.harness.models import Runner

    return Runner.objects.filter(pk=rid, owner=request.user).first()


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


# ---- zero-knowledge vault (Canopy Vault Phase 2, canopy-web#1550) ------------------
# Formats: docs/vault/zk-v1/zk_v1_reference.py. canopy-web only shape-checks and
# stores; the browser and the canopy CLI do every encryption (apps/agents/zk.py).
# Owner/admin only. Writes from a browser need a passkey step-up; a token held
# by an admin may enable, set values, grant and rotate (the canopy CLI acts as
# its device) — but turning ZK OFF hands plaintext back, so it is browser-only.

def _zk_problem(exc) -> ProblemError:
    return ProblemError(exc.status, "Zero-knowledge vault", type_=(
        TYPE_CONFLICT if exc.status == 409 else TYPE_VALIDATION), detail=str(exc),
        members={"error": exc.code})


def _zk_admin(request: HttpRequest, slug: str, *, browser_only: bool = False):
    agent = _agent_for_admin(request, slug)
    if browser_only or stepup.is_browser(request):
        stepup.require_recent_step_up(request)
    return agent


def _zk_state(agent, *, include_values: bool = False) -> ZkStateOut:
    from . import zk

    return ZkStateOut(**zk.state(agent, include_values=include_values))


@router.get("/{slug}/vault/zk", response=ZkStateOut,
            summary="Zero-knowledge state: kv, wraps, and who still needs one")
def get_agent_zk_state(request: HttpRequest, slug: str, values: bool = False) -> ZkStateOut:
    """`pending` lists active device keys (with an ECDH key) on runners this
    agent's resolve accepts, and current agent admins with a vault key, that
    hold no wrap at the current `kv` — each with the public key to wrap to.
    `wraps` are public data. `?values=true` adds the `zk1:` ciphertexts (what
    rotate and turn-off need), and records a read of each."""
    agent = _agent_for_admin(request, slug)
    out = _zk_state(agent, include_values=values)
    if values and out.values:
        services.record_credential_reads(agent, list(out.values), user=request.user, via="zk-state")
    return out


@router.post("/{slug}/vault/zk/enable", response=ZkStateOut,
             summary="Turn on zero-knowledge: wraps + every value as ciphertext, atomically")
def enable_agent_zk(request: HttpRequest, slug: str, payload: ZkEnableIn) -> ZkStateOut:
    """`{kv: 1, wraps: [{kind, target, wrap}], values: [{name, value_zk}]}`.
    Needs exactly one `recovery` wrap and a wrap the caller can open (their
    member wrap, or a device wrap on a runner they own — the CLI enabling as
    its device; the owner's member wrap can be granted later). `values` must cover every stored credential; each one's
    custodial copy is wiped. Errors are problem+json with an `error` code."""
    from . import zk

    agent = _zk_admin(request, slug)
    try:
        zk.enable(agent, kv=payload.kv, wraps=payload.wraps, values=payload.values, user=request.user)
    except zk.ZkError as exc:
        raise _zk_problem(exc) from exc
    agent.refresh_from_db()
    return _zk_state(agent)


@router.put("/{slug}/vault/zk/values", response=ZkValuesOut,
            summary="Set zero-knowledge values (ciphertext) at the current kv")
def set_agent_zk_values(request: HttpRequest, slug: str, payload: ZkValuesIn) -> ZkValuesOut:
    """Non-clobbering upsert of `{name, value_zk}`. A `kv` other than the
    current one is 409 `zk_kv_mismatch` (the vault key was rotated)."""
    from . import zk

    agent = _zk_admin(request, slug)
    try:
        names = zk.set_values(agent, kv=payload.kv, values=payload.values, user=request.user)
    except zk.ZkError as exc:
        raise _zk_problem(exc) from exc
    return ZkValuesOut(kv=payload.kv, names=names)


@router.post("/{slug}/vault/zk/wraps", response=ZkStateOut,
             summary="Grant: wrap the vault key to pending devices or admins")
def add_agent_zk_wraps(request: HttpRequest, slug: str, payload: ZkWrapsIn) -> ZkStateOut:
    """`{kv, wraps: [{kind: device|member, target, wrap}]}`. A device target is
    an ACTIVE key with an ECDH key on a runner this agent routes to; a member
    target is a current agent admin with a vault key. Replaces that target's
    wrap at this kv. The recovery wrap is set only by enable and rotate."""
    from . import zk

    agent = _zk_admin(request, slug)
    try:
        zk.add_wraps(agent, kv=payload.kv, wraps=payload.wraps, user=request.user)
    except zk.ZkError as exc:
        raise _zk_problem(exc) from exc
    return _zk_state(agent)


@router.post("/{slug}/vault/zk/rotate", response=ZkStateOut,
             summary="Rotate the vault key: replace every wrap and value atomically")
def rotate_agent_zk(request: HttpRequest, slug: str, payload: ZkRotateIn) -> ZkStateOut:
    """`{new_kv: kv + 1, wraps, values}`. Every target holding a wrap now that
    still qualifies must get one at `new_kv` (422 `zk_rewrap_required`
    otherwise); a revoked device or removed admin is simply left out. A fresh
    recovery wrap and a wrap the caller can open are required, and `values`
    must cover every stored credential."""
    from . import zk

    agent = _zk_admin(request, slug)
    try:
        zk.rotate(agent, new_kv=payload.new_kv, wraps=payload.wraps, values=payload.values,
                  user=request.user)
    except zk.ZkError as exc:
        raise _zk_problem(exc) from exc
    agent.refresh_from_db()
    return _zk_state(agent)


# ---- sender trust: a temporary exception to email alignment ----------------------
# See agents.models.SenderTrust. Agent-admin to write: it decides who an email
# is, and so what the agent's full profile answers to.

def _sender_trust_out(rule) -> SenderTrustOut:
    from django.utils import timezone

    return SenderTrustOut(
        email=rule.email, reason=rule.reason, expires_at=rule.expires_at,
        active=rule.expires_at > timezone.now(),
        created_by=rule.created_by.email if rule.created_by_id else None,
        created_at=rule.created_at,
    )


@router.get("/{slug}/sender-trust", response=list[SenderTrustOut],
            summary="List temporary trust rules for members' unaligned email")
def list_sender_trust(request: HttpRequest, slug: str) -> list[SenderTrustOut]:
    """Addresses whose mail is tied to their member account although it is not
    DMARC- or DKIM-aligned — each with why, and when it lapses (expired rules
    are listed with `active: false` until removed)."""
    from .models import SenderTrust

    agent = _get_agent_or_404(request, slug)
    return [_sender_trust_out(r) for r in
            SenderTrust.objects.filter(agent=agent).select_related("created_by")]


@router.put("/{slug}/sender-trust/{email}", response=SenderTrustOut,
            summary="Temporarily trust one member's unaligned email")
def set_sender_trust(request: HttpRequest, slug: str, email: str,
                     payload: SenderTrustIn) -> SenderTrustOut:
    """For `days` (1–90), mail From: this exact address is tied to its member
    account although it is not DMARC- or DKIM-aligned — as long as it still
    carries a passing DKIM signature, and the address still belongs to exactly
    one member of the agent's workspace. For a member whose domain signs with
    its provider's key (no DMARC, no own-domain DKIM) while that is fixed.
    Re-PUT to extend or reword; DELETE to end it early."""
    from django.utils import timezone

    from apps.harness.services import address_holder

    from .models import SenderTrust

    agent = _agent_for_admin(request, slug)
    address = (email or "").strip().lower()
    if "@" not in address:
        raise HttpError(422, f"not an email address: {email!r}")
    user, role = address_holder(address, agent.workspace_id)
    if user is None or role is None:
        raise HttpError(422, f"{address} does not belong to a member of this agent's "
                             "workspace; a trust rule only ties mail to a member")
    rule, _ = SenderTrust.objects.update_or_create(
        agent=agent, email=address,
        defaults={"reason": payload.reason.strip(),
                  "expires_at": timezone.now() + dt.timedelta(days=payload.days),
                  "created_by": request.user},
    )
    return _sender_trust_out(rule)


@router.delete("/{slug}/sender-trust/{email}", response={204: None},
               summary="End a temporary email trust rule")
def delete_sender_trust(request: HttpRequest, slug: str, email: str):
    """Their mail goes back to needing DMARC or own-domain DKIM. Idempotent."""
    from .models import SenderTrust

    agent = _agent_for_admin(request, slug)
    SenderTrust.objects.filter(agent=agent, email=(email or "").strip().lower()).delete()
    return Status(204, None)


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


# ---- Salesforce: another agent's own identity, lent to this one ---------------
# chrome-sales acts in Salesforce as one delegated identity (Eva's SF user). The
# row names the lender; the credential is the lender's own, resolved at use time.
# See the Salesforce section of apps/agents/delegations.py.

@router.get("/{slug}/salesforce", response=AgentSalesforceOut,
            summary="Whose Salesforce identity this agent borrows (masked — never the credential)")
def get_agent_salesforce(request: HttpRequest, slug: str) -> AgentSalesforceOut:
    agent = _get_agent_or_404(request, slug)
    return AgentSalesforceOut(**delegations.salesforce_status(agent))


@router.put("/{slug}/salesforce", response=AgentSalesforceOut,
            summary="Lend this agent another agent's Salesforce identity (owner of both)")
def set_agent_salesforce(request: HttpRequest, slug: str,
                         payload: AgentSalesforceIn) -> AgentSalesforceOut:
    """Checked before it is stored: the lender must hold a Salesforce credential
    and Salesforce must accept it. Refused with the reason otherwise."""
    agent = _get_agent_or_404(request, slug)
    try:
        delegations.set_salesforce(agent, request.user, payload.lender)
    except delegations.DelegationError as exc:
        raise HttpError(422, str(exc)) from exc
    return AgentSalesforceOut(**delegations.salesforce_status(agent))


@router.post("/{slug}/salesforce/check", response=AgentSalesforceOut,
             summary="Re-check the Salesforce identity this agent borrows")
def check_agent_salesforce(request: HttpRequest, slug: str) -> AgentSalesforceOut:
    agent = _get_agent_or_404(request, slug)
    delegations.check_salesforce(agent)
    return AgentSalesforceOut(**delegations.salesforce_status(agent))


@router.delete("/{slug}/salesforce", response=AgentSalesforceOut,
               summary="Stop lending this agent a Salesforce identity")
def delete_agent_salesforce(request: HttpRequest, slug: str) -> AgentSalesforceOut:
    agent = _get_agent_or_404(request, slug)
    delegations.clear_salesforce(agent, request.user)
    return AgentSalesforceOut(**delegations.salesforce_status(agent))


# Registered AFTER the literal `status`/`resolve` paths on purpose: Django
# resolves in order, so a `{name}` pattern declared first swallows "status" and
# answers 405 Method Not Allowed — the route exists, it is simply unreachable.
@router.delete("/{slug}/credentials/{name}", response=list[AgentCredentialStatusOut],
               summary="Remove one named secret")
def delete_agent_credential(request: HttpRequest, slug: str, name: str):
    agent = _agent_for_admin(request, slug)
    services.delete_agent_credential(agent, name)
    return services.agent_credential_status(agent)


@router.get("/{slug}/credentials/{name}/reads", response=list[AgentCredentialReadOut],
            summary="Who read this credential's value, from which box and turn")
def agent_credential_reads(request: HttpRequest, slug: str, name: str, limit: int = 100):
    """The read log (canopy-web#1538), newest first. Never the value. The
    agent's owner or an admin — the same people who may set the value."""
    agent = _agent_for_admin(request, slug)
    return services.credential_reads(agent, name, limit=max(1, min(limit, 500)))


@router.get("/{slug}/device-keys", response=list[RunnerDeviceKeyOut],
            summary="Runner device keys that could prove themselves for this agent")
def agent_device_keys(request: HttpRequest, slug: str):
    """Every device key on a runner this agent routes to or its owner owns —
    the keys `require_device_binding` would accept once active. `can_manage`
    says whether the caller may approve or revoke each (the runner's owner or
    an admin of it)."""
    from apps.harness import device_keys
    from apps.harness.api_device_keys import agents_key_opens, key_out, zk_agents_waiting_on
    from apps.harness.models import RunnerDeviceKey
    from apps.harness.runners import can_administer_runner

    agent = _agent_for_admin(request, slug)
    vault = [agent] if agent.zk_enabled and agent.credential_source == "canopy-web" else []
    out = []
    for k in device_keys.keys_for_agent(agent):
        row = key_out(k, can_manage=can_administer_runner(request.user, k.runner))
        row.can_open = agents_key_opens(k, vault)
        if row.can_manage and (k.status == RunnerDeviceKey.PENDING or k.ecdh_waiting):
            # Approving it will also wrap these (one passkey touch, canopy-web#1550).
            row.zk_pending_agents = zk_agents_waiting_on(request.user, k, assume_active=True)
        out.append(row)
    return out


@router.get("/{slug}/credential-alerts", response=list[AgentCredentialAlertOut],
            summary="Read-anomaly alerts on this agent's secrets")
def agent_credential_alerts(request: HttpRequest, slug: str, limit: int = 50):
    """Canopy Vault (canopy-web#1549), newest first: reads with no turn id by
    someone not the owner or an admin, reads of undeclared refs, bursts, and
    resolves refused for a bad or missing device proof. At most one per kind
    per hour (`occurrences` counts the rest). The agent's owner or an admin."""
    from . import vault_alerts

    agent = _agent_for_admin(request, slug)
    return vault_alerts.alerts_for(agent, limit=max(1, min(limit, 200)))


@router.get("/{slug}/pat-rotation", response=PatRotationOut,
            summary="This agent's canopy-pat rotation: setting, next due, history")
def get_pat_rotation(request: HttpRequest, slug: str) -> PatRotationOut:
    """Never a token. The agent's owner or an admin."""
    from . import pat_rotation

    agent = _agent_for_admin(request, slug)
    return PatRotationOut(**pat_rotation.status(agent))


@router.patch("/{slug}/pat-rotation", response=PatRotationOut,
              summary="Turn automatic canopy-pat rotation on or off")
def set_pat_rotation(request: HttpRequest, slug: str, payload: PatRotationIn) -> PatRotationOut:
    """Canopy Vault (canopy-web#1549). When on, every `period_days` (default
    30) canopy-web mints a new token for the agent's own canopy login, writes
    it to the agent's `canopy-pat` credential (a runner picks it up at its next
    `canopy cred sync`), keeps the old token valid for 24 h and then revokes it.
    Turning it on needs the agent to have its own login and a stored
    `canopy-pat`, and — when that login has ever signed in interactively — a
    workspace owner (422/403 otherwise). The owner or an admin."""
    from apps.events import audit

    from . import pat_rotation

    agent = _agent_for_admin(request, slug)
    before = (agent.pat_rotation_enabled, agent.pat_rotation_days)
    if payload.enabled:
        refusal = pat_rotation.enable_refusal(agent, request.user)
        if refusal:
            raise HttpError(refusal.status, str(refusal))
        if not agent.pat_rotation_enabled:
            agent.pat_rotation_enabled_by = request.user
    agent.pat_rotation_enabled = payload.enabled
    agent.pat_rotation_days = payload.period_days
    agent.save(update_fields=["pat_rotation_enabled", "pat_rotation_days",
                              "pat_rotation_enabled_by", "updated_at"])
    audit.note_change(scope=f"agent:{agent.slug}/pat-rotation",
                      before=f"{'on' if before[0] else 'off'}/{before[1]}d",
                      after=f"{'on' if payload.enabled else 'off'}/{payload.period_days}d")
    return PatRotationOut(**pat_rotation.status(agent))


@router.post("/{slug}/pat-rotation/rotate", response=PatRotationOut,
             summary="Rotate this agent's canopy-pat now")
def rotate_pat_now(request: HttpRequest, slug: str) -> PatRotationOut:
    """The same rotation the schedule does, now, whether or not rotation is on:
    a new token in `canopy-pat`, the old one valid for 24 h then revoked. It
    changes a secret, so from a browser it needs a fresh passkey step-up (403
    `step_up_required`); a token may call it. The owner or an admin."""
    from . import pat_rotation

    agent = _agent_for_admin(request, slug)
    if stepup.is_browser(request):
        stepup.require_recent_step_up(request)
    try:
        pat_rotation.rotate(agent, trigger="manual", actor=request.user)
    except pat_rotation.RotationRefused as exc:
        raise HttpError(exc.status, str(exc)) from exc
    agent.refresh_from_db()
    return PatRotationOut(**pat_rotation.status(agent))


@router.post("/{slug}/credentials/{name}/reveal", response=AgentCredentialRevealOut,
             summary="PLAINTEXT — show one stored value to an admin who just confirmed a passkey")
def reveal_agent_credential(request: HttpRequest, response: HttpResponse, slug: str, name: str):
    """Owner/agent admin, at a browser, with a passkey step-up in the last five
    minutes (403 `step_up_required` otherwise). Returns the value once and
    records who saw it; tokens are refused — a machine reads values through
    `/credentials/resolve`."""
    # The one deliberate exception to "a browser never sees plaintext"
    # (canopy-web#1538). What makes it safe is the conjunction: agent admin (who
    # could already overwrite the value and route a box of their own to read it)
    # AND a person present at the browser (step-up) AND a durable record.
    from .models import ZeroKnowledgeValue

    agent = _agent_for_admin(request, slug)
    stepup.require_recent_step_up(request)
    try:
        value = services.reveal_agent_credential(agent, name, user=request.user)
    except ZeroKnowledgeValue as exc:
        raise ProblemError(409, "Zero-knowledge value", type_=TYPE_CONFLICT, detail=str(exc),
                           members={"error": "zk_enabled"}) from exc
    if value is None:
        raise ProblemError(404, "Credential not found", type_=TYPE_NOT_FOUND,
                           detail=f"{agent.slug} has no value stored under {name!r}")
    response["Cache-Control"] = "no-store"
    return AgentCredentialRevealOut(name=name, value=value)


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


@router.get("/{slug}/setup", response=list[AgentSetupStepOut],
            summary="What is left before this agent can do its job")
def agent_setup(request: HttpRequest, slug: str) -> list[AgentSetupStepOut]:
    """The setup checklist, in order: repository, GitHub identity, mailbox, vault, runner,
    its first real skill (done in Claude Code — `command` starts that session), first
    turn. Each step reads the same check the system runs for that thing."""
    from .setup import setup_steps

    agent = _get_agent_or_404(request, slug)
    return [AgentSetupStepOut(**s) for s in setup_steps(agent)]


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
    # A runner token speaks for ONE box: it reports only for an agent that box
    # serves, never for the owner's other agents (same rule as credentials).
    rtok = getattr(request, "runner_token", None)
    if rtok is not None:
        from apps.harness.device_keys import runner_serves_agent

        if not runner_serves_agent(rtok.runner, agent):
            raise HttpError(403, f"{rtok.runner.name} does not run {agent.slug}")
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

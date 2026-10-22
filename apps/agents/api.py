"""Django Ninja routers for the /api/agents surface — a first-class AI-agent
workspace. This module holds the agent itself (create, read, move, delete, its
identity, admins, licenses and settings) and its syncs and turns; the rest of
the surface is split by subdomain into the `api_*.py` modules beside it:
`api_routing` (which runners do its work), `api_skills`, `api_board` (projects,
tasks, the action queue), `api_credentials` (secrets, vault, zero-knowledge,
GitHub, Salesforce, PAT rotation) and `api_readiness`.

The turn routes stay HERE, on `turns_router`: `list_turns` shares its name with
a harness view, so its operationId (= MCP tool name) is qualified by this
module's app label (`agents_list_turns`), and moving it would change it."""
from __future__ import annotations

import requests
from django.http import HttpRequest
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
from apps.canopy_sessions.services import close_agent_sessions
from apps.events import audit
from apps.harness import closeout, turn_access
from apps.harness import initiator as who
from apps.harness.models import Turn
from apps.passkeys import stepup
from apps.plugins import services as plugin_services
from apps.slack import commands as slack_commands
from apps.slack.models import SlackInstallation
from apps.tokens.middleware import minting_refusal
from apps.workspaces import permissions as perms
from apps.workspaces import services as wsvc

from . import access, creation, delegations, mailbox, services
from . import move as agent_move
from .creation import repo_url
from .credentials import licenses, zk
from .gates import (
    agent_for_admin,
    agent_for_read,
    agent_for_work,
    listed_agent_workspaces,
)
from .interface import InterfaceError, domain_warnings, parse
from .models import (
    AgentAdmin,
    AgentCredentialRead,
    AgentLicense,
)
from .route_refusals import refuse_auto_unless_admin, zk_problem
from .schemas import (
    AgentAccessOut,
    AgentAdminOut,
    AgentCanopyLoginOut,
    AgentCanopyTokenIn,
    AgentCanopyTokenOut,
    AgentCanopyUserIn,
    AgentDetailOut,
    AgentIn,
    AgentInterfaceIn,
    AgentInterfaceOut,
    AgentLicenseOut,
    AgentLicenseRevokeIn,
    AgentMoveIn,
    AgentMoveOut,
    AgentOut,
    AgentOwnerIn,
    AgentPluginOut,
    AgentRuntimeOut,
    AgentScaffoldIn,
    AgentScaffoldOut,
    AgentSyncIn,
    AgentSyncOut,
    AgentTurnIn,
    AgentTurnOut,
    CredentialSourceIn,
    DeviceBindingIn,
    RepoCheckIn,
    RepoCheckOut,
    RepositoryIn,
    SlackEnabledIn,
    SlackEnabledOut,
    TurnModeIn,
)
from .setup import request_setup

router = Router(auth=session_auth, tags=["agents"])


def _can(request: HttpRequest, workspace, capability: str) -> bool:
    """May the caller exercise `capability` in `workspace`? The request-shaped
    door to `apps/workspaces/permissions.py`, the one table of what each role
    may do. This module names capabilities, never roles."""
    return perms.can(request.user, workspace, capability)


def _plugin_out(agent, refs: dict[int, dict]) -> AgentPluginOut | None:
    ref = plugin_services.plugin_ref(agent, refs)
    return AgentPluginOut(**ref) if ref else None


@router.get("/", response=Page[AgentOut], summary="List agents",)
def list_agents(request: HttpRequest, limit: int = 100) -> Page[AgentOut]:
    limit = clamp_limit(limit)
    visible = listed_agent_workspaces(request)
    agents = [a for a in services.list_agents() if a.workspace_id in visible]
    refs = plugin_services.plugin_refs(request.user) if agents else {}
    return paginate(agents, offset=0, limit=limit, map=lambda a: AgentOut.model_validate(a).model_copy(
        update={"plugin": _plugin_out(a, refs)}))


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

    # This is the reshaping tier (same as agent_for_work), but there is no
    # existing agent to resolve through agent_for_read on a create — so the
    # gate is against the TARGET workspace directly: an already-existing
    # agent's CURRENT home (this write reshapes that tenant's row, whatever
    # workspace the caller happens to default into), or `home` for a
    # brand-new agent — which is the same thing, `home` above.
    role = wsvc.member_role(request.user, home)
    if existing is not None and role is None:
        # Resolve-then-authorize, the ordering `agent_for_work` gets for free
        # from `agent_for_read`. It has to be spelled out here because this
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
    # (`delegations.set_github` is for its owner and admins) and no box may hold it unless a workspace
    # owner pairs one, so leaving it unset left every new agent stuck until someone ran
    # `transfer_owner`. Applied on create only; an existing agent keeps its owner.
    agent = services.upsert_agent(payload, workspace=home, owner=request.user)
    if existing is None or (payload.repo_url is not None and payload.repo_url != existing.repo_url):
        # A cloud box only learns of an agent (or its new repo) by re-running its
        # bootstrap; ask the ones that will take its turns to do that now.

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
    agent = agent_for_read(request, slug)
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

    try:
        full = creation.parse_repo(raw_repo)
    except creation.ScaffoldError as exc:
        raise ProblemError(exc.status, str(exc), type_=TYPE_VALIDATION) from None
    return _scaffold_slug(request, workspace, raw_slug, full), full


def _inspect(token: str, full: str, slug: str):
    """Who the token is and what the repo looks like through it, or a problem."""

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
            mailbox=payload.mailbox.strip(), google_account=payload.google_account.strip(),
            stakeholders=payload.stakeholders.strip(),
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
    agent = agent_for_read(request, slug)
    return _detail(request, agent)


# Browser-only, like owner and admin changes: linking a canopy user makes it
# count as the agent itself (never confined against itself — agents/access.py),
# so it is a decision a PERSON makes in the canopy UI. The owner or an admin.
@router.put("/{slug}/canopy-user", response=AgentDetailOut,
            summary="Link this agent to the canopy user it is")
def link_canopy_user(request: HttpRequest, slug: str, payload: AgentCanopyUserIn) -> AgentDetailOut:
    """The canopy user account this agent's own token signs in as. `user_id`
    null unlinks it."""
    agent = agent_for_read(request, slug)
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

    agent = agent_for_admin(request, slug)
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

    agent = agent_for_admin(request, slug)
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
    agent = agent_for_read(request, slug)
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
    iface = agent.interface or {}
    return {"interface": iface, "source": agent.interface_source or "",
            "published_at": agent.interface_published_at,
            "published_by_email": by.email if by is not None else None,
            # Computed AFTER any save, cached and time-boxed: it never blocks one.
            "warnings": domain_warnings(iface, agent.workspace_id) if iface else []}


@router.get("/{slug}/interface", response=AgentInterfaceOut,
            summary="What this agent offers callers — its declared interface")
def get_interface(request: HttpRequest, slug: str):
    return _interface_out(agent_for_read(request, slug))


# Owner-or-admin, not the editor tier the skill catalog uses: the interface is
# a SECURITY POLICY — it decides what people outside the agent's admins can make
# it do — so loosening it is the same kind of act as handing over its keys.
@router.put("/{slug}/interface", response=AgentInterfaceOut,
            summary="Save the agent's declared interface (YAML source, or a parsed mapping)")
def publish_interface(request: HttpRequest, slug: str, payload: AgentInterfaceIn):
    import yaml
    from django.utils import timezone

    agent = agent_for_admin(request, slug)
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
    agent = agent_for_admin(request, slug)
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
    return _admin_rows(agent_for_read(request, slug))


@router.get("/{slug}/access", response=AgentAccessOut,
            summary="Everyone's role on this agent, why, and what they can reach")
def agent_access(request: HttpRequest, slug: str):
    """Every member of the agent's workspace with their role on the agent
    (owner / admin / member), the reason for it, and what they reach signed in:
    the whole agent, the capabilities its published interface lists for them,
    or nothing. `outsiders` lists the interface rules that reach people outside
    the workspace. Readable by any member, like the admin list."""

    agent = agent_for_read(request, slug)
    iface = agent.interface or {}
    return {
        "members": access.roster(agent),
        "outsiders": access.outsiders(agent),
        "interface_published": bool(iface.get("capabilities") or iface.get("full")),
        "slack_enabled": agent.slack_enabled,
    }


def _license_rows(agent) -> list[dict]:
    from django.db.models import Count

    lics = list(AgentLicense.objects.filter(agent=agent)
                .select_related("user", "issued_by", "revoked_by"))
    users = [lic.user_id for lic in lics]
    turns = Turn.objects.filter(agent=agent, origin=Turn.ORIGIN_LOCAL, initiator_user_id__in=users)
    counts = dict(turns.values_list("initiator_user_id").annotate(n=Count("id")))
    machines: dict[int, list[str]] = {}
    for uid, name in (turns.exclude(private_runner=None).order_by("-created_at")
                      .values_list("initiator_user_id", "private_runner__name")):
        if name not in machines.setdefault(uid, []):
            machines[uid].append(name)
    reads = dict(AgentCredentialRead.objects.filter(license__in=lics)
                 .values_list("license_id").annotate(n=Count("id")))
    return [{
        "user_id": lic.user_id, "email": lic.user.email,
        "name": lic.user.get_full_name() or lic.user.email,
        "status": lic.status, "basis": lic.basis, "note": lic.note,
        "requested_at": lic.requested_at, "issued_at": lic.issued_at,
        "issued_by_email": getattr(lic.issued_by, "email", None),
        "revoked_at": lic.revoked_at, "revoked_by_email": getattr(lic.revoked_by, "email", None),
        "last_used_at": lic.last_used_at, "private_runners": machines.get(lic.user_id, []),
        "local_turns": counts.get(lic.user_id, 0), "credential_reads": reads.get(lic.pk, 0),
    } for lic in lics]


@router.get("/{slug}/licenses", response=list[AgentLicenseOut],
            summary="Who uses this agent locally: its license holders")
def list_agent_licenses(request: HttpRequest, slug: str):
    """Every license to use the agent in someone's own Claude Code — active,
    requested (the caller list did not allow it yet) or revoked — with the
    private_runners it ran on, its local turns and credential reads, and when it was
    last used. Readable by any member, like the admin list."""
    return _license_rows(agent_for_read(request, slug))


def _license_target(request: HttpRequest, slug: str, user_id: int):
    from django.contrib.auth import get_user_model

    agent = agent_for_read(request, slug)
    if not agent.is_admin(request.user):
        raise HttpError(403, f"only an admin of {agent.slug} may change who uses it locally")
    target = get_user_model().objects.filter(pk=user_id).first()
    if target is None:
        raise HttpError(404, "user not found")
    return agent, target


@router.post("/{slug}/licenses/{user_id}/revoke", response=AgentLicenseOut,
             summary="Revoke a person's license to use this agent locally")
def revoke_agent_license(request: HttpRequest, slug: str, user_id: int,
                         payload: AgentLicenseRevokeIn | None = None):
    """Per person, without editing the caller list: they may not open a local turn
    or read the agent's credentials again until an admin approves them. A revoked
    license is never re-issued automatically."""

    agent, target = _license_target(request, slug, user_id)
    before = AgentLicense.objects.filter(agent=agent, user=target).values_list("status", flat=True).first()
    licenses.revoke(agent, target, by=request.user, note=(payload.note if payload else ""))
    audit.note_change(scope=f"agent.license:{target.email}", before=before,
                      after=AgentLicense.REVOKED, workspace=agent.workspace)
    return next(r for r in _license_rows(agent) if r["user_id"] == target.pk)


@router.post("/{slug}/licenses/{user_id}/approve", response=AgentLicenseOut,
             summary="Issue or reinstate a person's license to use this agent locally")
def approve_agent_license(request: HttpRequest, slug: str, user_id: int):
    """Re-asks the caller list: refused (409) unless it gives them the whole agent —
    make them an editor or add a `full:` rule first."""

    agent, target = _license_target(request, slug, user_id)
    before = AgentLicense.objects.filter(agent=agent, user=target).values_list("status", flat=True).first()
    try:
        licenses.approve(agent, target, by=request.user)
    except licenses.LicenseRefusedError as exc:
        raise HttpError(409, str(exc)) from exc
    audit.note_change(scope=f"agent.license:{target.email}", before=before,
                      after=AgentLicense.ACTIVE, workspace=agent.workspace)
    return next(r for r in _license_rows(agent) if r["user_id"] == target.pk)


# Granting admin hands over the agent's credentials, so it is held to the same
# bar as transferring the agent: its owner or a workspace owner (by browser or
# MCP alike). Resolve first so a non-member gets 404.
def _admin_change_gate(request: HttpRequest, slug: str):
    agent = agent_for_read(request, slug)
    if not _may_manage_admins(request, agent):
        raise HttpError(403, "only the agent's owner or a workspace owner can change its admins")
    return agent


@router.put("/{slug}/admins/{user_id}", response=list[AgentAdminOut],
            summary="Make a workspace member an admin of this agent")
def grant_admin(request: HttpRequest, slug: str, user_id: int):
    from django.contrib.auth import get_user_model

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

    Gated at the same reshaping tier as everything else `agent_for_work`
    covers (Phase 0 of the agent-instances-and-ACL design closed the old gap
    where deletion was the ONLY gated write on this surface — everything else,
    including the credential/vault writers, was membership-or-nothing): a
    viewer cannot destroy a fleet member's board. `agent_for_work` resolves
    the agent via `agent_for_read` first, so a non-member gets 404 (no
    existence leak) rather than 403.

    Every FK into Agent is CASCADE or SET_NULL (runs, turns, tasks, skills,
    syncs, projects, schedules, task actions, runner assignments/drills), so
    this is a real delete rather than a soft flag — nothing is left dangling.
    One thing blocks it: a workspace project the agent stewards (409). Its open chat sessions are closed first: the runner
    deletes each emdash task, and an offline runner does so when it is back.
    """

    agent = agent_for_work(request, slug)
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


@router.patch("/{slug}/turn-mode", response=AgentDetailOut,
              summary="Set an agent's turn mode (manual | auto)")
def set_turn_mode(request: HttpRequest, slug: str, payload: TurnModeIn) -> AgentDetailOut:
    """Flip the agent's runtime autonomy posture — the board-side switch the
    fleet turn procedure reads at preflight (agent-core/turn.md § Turn mode).
    A human decision made from the board; the agent-repo upsert (POST /) cannot
    touch this field. `manual` from any editor; `auto` from the agent's owner or
    an admin — it is the last rung of the routing ladder ("Everything else")."""
    agent = agent_for_work(request, slug)
    if payload.turn_mode == "auto" and agent.turn_mode != "auto":
        refuse_auto_unless_admin(request, agent, "setting the agent's turn mode")
    agent.turn_mode = payload.turn_mode
    agent.save(update_fields=["turn_mode", "updated_at"])
    return _detail(request, agent)


@router.patch("/{slug}/repository", response=AgentDetailOut,
              summary="Point an agent at its GitHub repository")
def set_repository(request: HttpRequest, slug: str, payload: RepositoryIn) -> AgentDetailOut:
    """Set the repository (and optionally the branch) its runners clone and run —
    the one field the setup checklist's first step asks for, without the create
    wizard. A github.com URL in any spelling; stored as `https://github.com/owner/repo`.

    The agent's owner or an admin (`agent_for_admin`): the repo is what a box
    runs as this agent, the same bar the upsert holds `DEFINITION_FIELDS` to. The
    cloud boxes that take its turns are asked to re-run their bootstrap, as a
    repo change through the upsert does."""

    agent = agent_for_admin(request, slug)
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

    The agent's owner or an admin only (`agent_for_admin`), like the
    credentials and the vault pointer themselves: choosing where an agent's
    secrets come from is part of controlling them. Absent from AgentIn, so the
    repo's self-publish upsert cannot move it. From a browser it needs a fresh
    passkey step-up, like every other change to where secrets live."""
    agent = agent_for_admin(request, slug)
    if stepup.is_browser(request):
        stepup.require_recent_step_up(request)
    if payload.credential_source == "canopy-web":
        # Zero-knowledge is the canopy-web vault's only mode (Jonathan,
        # 2026-10-17): moving here seals every stored value to the agent's
        # readers and wipes the custodial copies, or refuses when nobody could
        # read them yet.

        try:
            services.make_zero_knowledge(agent)
        except zk.ZkError as exc:
            raise zk_problem(exc) from exc
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
    agent = agent_for_admin(request, slug)
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

    agent = agent_for_admin(request, slug)
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
    agent = agent_for_read(request, slug)
    return AgentRuntimeOut(
        slug=agent.slug,
        repo_url=agent.repo_url,
        repo_ref=agent.repo_ref,
        engine=agent.runtime_engine,
        secret_refs=list(agent.runtime_secrets or []),
        workspace=agent.workspace_id,
    )


turns_router = Router(auth=session_auth, tags=["agents"])


# ---- syncs (Google-Doc backed) ----
@turns_router.get("/{slug}/syncs/", response=Page[AgentSyncOut], summary="List the agent's syncs",)
def list_syncs(request: HttpRequest, slug: str, limit: int = 100) -> Page[AgentSyncOut]:
    limit = clamp_limit(limit)
    agent = agent_for_read(request, slug)
    items = [AgentSyncOut.model_validate(s) for s in services.list_syncs(agent, limit=limit)]
    return paginate(items, offset=0, limit=limit)


@turns_router.post("/{slug}/syncs/", response={201: AgentSyncOut},
             summary="Post a Google-Doc sync (idempotent per period+source)",)
def create_sync(request: HttpRequest, slug: str, payload: AgentSyncIn) -> Status:
    agent = agent_for_work(request, slug)
    sync = services.upsert_sync(agent, payload)
    return Status(201, AgentSyncOut.model_validate(sync))


@turns_router.delete("/{slug}/syncs/{sync_id}/", response={204: None},
               summary="Delete a sync (wrong period / stray record)",)
def delete_sync(request: HttpRequest, slug: str, sync_id: int) -> Status:
    """POST upserts per (period, source), so re-posting only corrects a sync for the
    SAME window — a sync filed under the wrong period is otherwise unreachable."""
    agent = agent_for_work(request, slug)
    if not services.delete_sync(agent, sync_id):
        raise HttpError(404, f"sync {sync_id} not found for agent '{slug}'")
    return Status(204, None)


# ---- turns (a packaged unit of work + optional transcript link) ----
@turns_router.get("/{slug}/turns/", response=Page[AgentTurnOut], summary="List the agent's turns",)
def list_turns(request: HttpRequest, slug: str, limit: int = 100) -> Page[AgentTurnOut]:
    limit = clamp_limit(limit)
    agent = agent_for_read(request, slug)

    # Everyone sees what the agent did; a turn's prompt and transcript link are
    # a log (turn_access).
    turns = turn_access.redact(list(closeout.list_turns(agent, limit=limit)), request.user)
    closeout.stamp_share_urls(turns)
    items = [AgentTurnOut.model_validate(t) for t in turns]
    return paginate(items, offset=0, limit=limit)


@turns_router.post("/{slug}/turns/", response={201: AgentTurnOut},
             summary="Package a turn (idempotent per cli_session_id)",)
def create_turn(request: HttpRequest, slug: str, payload: AgentTurnIn) -> Status:
    agent = agent_for_work(request, slug)

    # A report-only row (no dispatch matched) is a turn someone ran by hand; the
    # caller posting its close-out is the best account of who that was.
    turn = closeout.upsert_turn(agent, payload,
                                initiator=who.for_request(request, via="closeout"))
    closeout.stamp_share_urls([turn])
    return Status(201, AgentTurnOut.model_validate(turn))

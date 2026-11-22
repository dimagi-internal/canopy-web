"""/api/agents/stored and /api/agents/{slug}/definition… — a stored agent's definition.

Who may do what, and why (docs/architecture/stored-agents.md):

* reading a definition is reading the agent (`agent_for_read`); its archive is also open
  to a RUNNER that may hold the agent (`runner_may_hold_agent`), for live and published
  revisions only, never a draft;
* a draft is `agent_edit` on the agent, and its own login may write one: an edit by an
  agent (its login, or any turn's token) files an approval request instead of reaching
  anything live — unless that agent may publish it itself (below), when it asks for none;
* publish and rollback are the agent's owner or admins acting as THEMSELVES, an agent
  acting AS one of them under an act-as-you permission they approved with their passkey
  (live, `write`, naming no other turn), or ANOTHER agent's own login (its PAT or OAuth,
  or its turn's login) when that agent administers this one
  (`acting_person.person_act_as_or_admin_agent`). Refused to the agent's own login, even
  when it is its own admin through a workspace role; to caller, contact and runner tokens;
  to `X-Canopy-On-Behalf-Of` without a permission; and to an agent that is not an admin.
  The revision and the response name an admin agent (`published_by_agent`, `published_by`
  "<slug> (agent)") and an act-as agent (`acted_by`);
* convert, export and detach change where the definition lives, so they stay a person's
  act alone, or an act-as agent's for them (`acting_person.person_or_act_as`): never an
  agent's own login, an admin or not;
* once the definition moved to GitHub, every write here is 409 `exported`.

The repository itself is served read-only over git by `git_http`.
"""
from __future__ import annotations

import base64
import binascii
import uuid

from canopy_agent_factory import AgentFactoryError, normalize_slug
from canopy_sdk import agent_definition as policy
from django.http import HttpRequest, HttpResponse
from ninja import Router, Status
from ninja.errors import HttpError

from apps.agents import access, creation
from apps.agents import definition_store as store
from apps.agents.creation import RESERVED_SLUGS, NoOwnerError, creating_owner
from apps.agents.gates import agent_for_admin, agent_for_edit, agent_for_read
from apps.agents.holding import runner_may_hold_agent
from apps.agents.models import Agent, AgentDefinitionRevision
from apps.api.auth import session_auth
from apps.api.errors import (
    TYPE_CONFLICT,
    TYPE_FORBIDDEN,
    TYPE_NOT_FOUND,
    TYPE_PAYLOAD_TOO_LARGE,
    TYPE_UPSTREAM,
    TYPE_VALIDATION,
    ProblemError,
    refusals,
)
from apps.api.route_gates import gate
from apps.common import request_context
from apps.events import audit
from apps.harness import acting_person
from apps.harness.models import Runner, Turn
from apps.workspaces import permissions as perms
from apps.workspaces import services as wsvc

from . import services
from .schemas import (
    DefinitionActedByOut,
    DefinitionAuthorOut,
    DefinitionConvertIn,
    DefinitionDetachIn,
    DefinitionDiffOut,
    DefinitionDraftIn,
    DefinitionExportIn,
    DefinitionMovedOut,
    DefinitionOut,
    DefinitionPolicyOut,
    DefinitionRevisionOut,
    DefinitionRevisionSummaryOut,
    DefinitionRollbackIn,
    StoredAgentIn,
    StoredAgentOut,
)

router = Router(auth=session_auth, tags=["agents"])

_TYPES = {404: TYPE_NOT_FOUND, 403: TYPE_FORBIDDEN, 409: TYPE_CONFLICT, 413: TYPE_PAYLOAD_TOO_LARGE,
          422: TYPE_VALIDATION, 502: TYPE_UPSTREAM}


def _problem(exc: services.Refused) -> ProblemError:
    members = {"error": exc.code} if exc.code else {}
    if exc.problems:
        members["problems"] = exc.problems
    return ProblemError(exc.status, str(exc), type_=_TYPES.get(exc.status, TYPE_CONFLICT), members=members)


def _require_person(request: HttpRequest, agent: Agent, what: str):
    """`what` is a person's own act (convert, export, detach: where the definition lives):
    never an agent's login, an admin agent's included, never a turn's token. A person
    acting as themselves passes (None), and so does an agent acting AS the agent's owner or
    an admin under a live, `write` act-as-you permission that names no other turn (the
    permission, `harness.acting_person.person_or_act_as`)."""
    got = acting_person.person_or_act_as(request)
    why, grant = (got, None) if isinstance(got, str) else (None, got[1])
    if why is None and agent.is_own_login(request.user):
        why = "agent_login"
    if why is None and grant is not None and not agent.is_admin(grant.person):
        why = "act_as_not_admin"
    if why is not None:
        raise ProblemError(403, f"{what} is a person's act", type_=TYPE_FORBIDDEN,
                           detail=(f"{what} changes what runs with {agent.slug}'s credentials, so only its owner "
                                   f"or an admin may do it: as themselves, or through an agent they let act as "
                                   f"them with an act-as-you permission (read + write) — not an agent's own "
                                   f"login or a turn's token, even one of an agent that administers it ({why})."),
                           members={"error": "not_a_person", "reason": why})
    _note_acting(request, grant)
    return grant


def _require_publisher(request: HttpRequest, agent: Agent, what: str):
    """`what` (publish, roll back) is a person's act, an act-as agent's for them, or ANOTHER
    agent's that administers `agent` (`acting_person.person_act_as_or_admin_agent`). Returns
    `(grant, admin_agent)`: the act-as permission, or the admin agent acting as itself."""
    got = acting_person.person_act_as_or_admin_agent(request, agent)
    why, grant, acting = (got, None, None) if isinstance(got, str) else (None, got[1], got[2])
    if why is None and acting is None and agent.is_own_login(request.user):
        why = "agent_login"
    if why is None and grant is not None and not agent.is_admin(grant.person):
        why = "act_as_not_admin"
    if why is not None:
        raise ProblemError(403, f"{what} is for a person or an admin agent", type_=TYPE_FORBIDDEN,
                           detail=(f"{what} changes what runs with {agent.slug}'s credentials, so only its owner "
                                   f"or an admin may do it: as themselves, through an agent they let act as them "
                                   f"with an act-as-you permission (read + write), or another agent that "
                                   f"administers {agent.slug}, signed in as itself. Never {agent.slug}'s own "
                                   f"login, a turn's caller token, or an agent that is not its admin ({why})."),
                           members={"error": "not_a_publisher", "reason": why})
    if acting is not None:
        audit.note_acting(request, {"agent": acting.slug, "admin_of": agent.slug})
    else:
        _note_acting(request, grant)
    return grant, acting


def _acted_by(grant) -> DefinitionActedByOut | None:
    """The agent that did a person's act as them, and the permission it held, or None."""
    if grant is None:
        return None
    return DefinitionActedByOut(agent=grant.agent.slug, person=grant.person.email, act_as_grant=str(grant.pk))


def _revision_acted_by(rev: AgentDefinitionRevision) -> DefinitionActedByOut | None:
    """Who made `rev` (created it live, or published it) as a person, under act-as."""
    if not rev.acting_agent_id:
        return None
    if rev.act_as_grant_id:
        return _acted_by(rev.act_as_grant)
    person = rev.published_by or rev.author_user
    return DefinitionActedByOut(agent=rev.acting_agent.slug, person=getattr(person, "email", "") or "",
                                act_as_grant="")


def _who(request: HttpRequest, grant) -> str:
    """The actor, for an audit summary: "<agent> as <person>" under act-as."""
    email = getattr(request.user, "email", "") or ""
    return f"{grant.agent.slug} as {email}" if grant is not None else email


_REVISION_RELATED = ("parent", "author_user", "published_by", "acting_agent", "act_as_grant__agent",
                     "act_as_grant__person", "published_by_agent")


def _note_acting(request: HttpRequest, grant) -> None:
    """The audit row says who did a person's act under act-as: the agent, and the permission."""
    if grant is not None:
        audit.note_acting(request, {"agent": grant.agent.slug, "act_as_grant": str(grant.pk),
                                    "for_id": grant.person_id, "for": grant.person.email})


def _stored_or_409(agent: Agent) -> None:
    if agent.definition_exported_to:
        raise ProblemError(409, f"{agent.slug}'s definition moved to GitHub", type_=TYPE_CONFLICT,
                           detail=(f"It runs from github.com/{agent.definition_exported_to} now; its canopy "
                                   "repository is a read-only archive. Change it there."),
                           members={"error": "exported", "repo": agent.definition_exported_to})
    if not agent.is_stored:
        raise ProblemError(409, f"{agent.slug} is not a stored agent", type_=TYPE_CONFLICT,
                           detail="Its definition is its git repository.", members={"error": "not_stored"})


def _authoring_turn(request: HttpRequest) -> Turn | None:
    """The turn a request says it comes from: `X-Canopy-Parent-Turn` (the canopy CLI's),
    `X-Canopy-Turn-Id`, or the turn a turn's own token was issued for."""
    candidates = [((request_context.current().get("parent") or {}).get("turn") or ""),
                  request.META.get(acting_person.TURN_HEADER, ""),
                  str((getattr(request, "auth_credential", None) or {}).get("turn_id") or "")]
    for raw in candidates:
        try:
            tid = uuid.UUID(str(raw).strip())
        except ValueError:
            continue
        turn = Turn.objects.filter(pk=tid).first()
        if turn is not None:
            return turn
    return None


def _summary(rev: AgentDefinitionRevision | None) -> DefinitionRevisionSummaryOut | None:
    if rev is None:
        return None
    tree = rev.tree or {}
    return DefinitionRevisionSummaryOut(
        number=rev.number, status=rev.status, root_sha256=rev.root_sha256, commit=rev.commit or "",
        message=rev.message,
        parent=rev.parent.number if rev.parent_id else None,
        author=DefinitionAuthorOut(email=getattr(rev.author_user, "email", "") or "", by_agent=rev.by_agent,
                                   turn_id=str(rev.author_agent_turn_id) if rev.author_agent_turn_id else None),
        files=len(tree), size=sum(int(e.get("size", 0)) for e in tree.values()),
        created_at=rev.created_at, published_at=rev.published_at,
        published_by=(f"{rev.published_by_agent.slug} (agent)" if rev.published_by_agent_id
                      else getattr(rev.published_by, "email", None) if rev.published_by_id else None),
        published_by_agent=rev.published_by_agent.slug if rev.published_by_agent_id else None,
        approval_id=rev.approval_id if rev.status == AgentDefinitionRevision.DRAFT else None,
        acted_by=_revision_acted_by(rev))


def _revision_or_404(agent: Agent, number: int) -> AgentDefinitionRevision:
    rev = (AgentDefinitionRevision.objects.select_related(*_REVISION_RELATED)
           .filter(agent=agent, number=number).first())
    if rev is None:
        raise ProblemError(404, f"{agent.slug} has no revision {number}", type_=TYPE_NOT_FOUND)
    return rev


# ---- creating a stored agent, and converting one ----------------------------


@router.post("/stored", response={201: StoredAgentOut, **refusals(409, 413, 422)},
             summary="Create an agent whose definition canopy stores (no GitHub repository)")
@gate("agent.work", setting=True)  # editor in the target workspace, the same gate as registering an agent
def create_stored_agent(request: HttpRequest, payload: StoredAgentIn) -> Status:
    """Stamps the agent from the canopy agent factory (the same scaffold `/canopy:create-agent`
    writes) and stores it as revision 1, published. No repository and no GitHub token: its
    runners materialize the definition from canopy. The creator owns it. An agent's own login
    creates it for a person, never for itself: the person who asked for the turn it works in
    (known from its turn login, or the `X-Canopy-Parent-Turn` it sends), else the agent's
    owner; 403 `no_owner` when neither may create agents in the workspace. 409 `exists` when
    the slug is taken.

    With `files`, revision 1 is the scaffold with those files laid over it, held to the file
    policy, and the files canopy writes itself rewritten: one the policy refuses refuses the
    create (422 `policy` with `problems`), or with `drop_refused` is left out and listed in
    `dropped`. With `bundle_b64` (a `git bundle` of a branch) the source's history is kept, as
    `convert` keeps it: under `refs/heads/imported`, every blob scanned for secrets first (422
    `secret`), revision 1 a commit on its tip. Files and history together are held to one
    request's size (413 `too_large`)."""
    workspace = payload.workspace.strip()
    if not wsvc.is_member(request.user, workspace):
        raise ProblemError(404, f"workspace '{workspace}' not found", type_=TYPE_NOT_FOUND)
    if not perms.can(request.user, workspace, perms.AGENT_WORK):
        raise ProblemError(403, "creating an agent requires the editor role or above", type_=TYPE_FORBIDDEN)
    try:
        slug = normalize_slug(payload.slug)
    except AgentFactoryError as exc:
        raise ProblemError(422, "That name cannot be an agent slug", type_=TYPE_VALIDATION, detail=str(exc)) from None
    if slug in RESERVED_SLUGS:
        raise ProblemError(422, "That name cannot be an agent slug", type_=TYPE_VALIDATION,
                           detail=f"'{slug}' is reserved; pick another")
    if Agent.objects.filter(slug=slug).exists():
        raise ProblemError(409, f"The slug '{slug}' is taken", type_=TYPE_CONFLICT,
                           detail="Agent slugs are unique across canopy. Pick another name.",
                           members={"error": "exists"})
    files: dict[str, tuple[bytes, bool]] = {}
    for f in payload.files:
        if f.path in files:
            raise ProblemError(422, f"{f.path} is listed twice", type_=TYPE_VALIDATION)
        if (f.content is None) == (f.content_b64 is None):
            raise ProblemError(422, f"{f.path}: send content or content_b64 (exactly one)", type_=TYPE_VALIDATION)
        data = f.content.encode("utf-8") if f.content is not None else _decode(f.content_b64, f.path)
        files[f.path] = (data, f.executable)
    bundle = _decode(payload.bundle_b64, "bundle_b64") if payload.bundle_b64 else None
    home = wsvc.Workspace.objects.get(slug=workspace)
    got = acting_person.person_or_act_as(request)
    grant = None if isinstance(got, str) else got[1]
    _note_acting(request, grant)
    try:
        owner = creating_owner(request, home)
    except NoOwnerError as exc:
        raise ProblemError(403, "No person can own this agent", type_=TYPE_FORBIDDEN, detail=str(exc),
                           members={"error": "no_owner"}) from None
    try:
        agent, dropped = services.create_stored_agent(
            slug=slug, name=payload.name.strip(), mandate=payload.mandate.strip(),
            stakeholders=payload.stakeholders.strip(), workspace=home,
            owner=owner, by=request.user, act_as=grant, files=files, bundle=bundle,
            message=payload.message.strip(), drop_refused=payload.drop_refused)
    except services.Refused as exc:
        raise _problem(exc) from None
    rev = agent.live_revision
    audit.note_change(scope=f"agent:{agent.slug}/definition", before=None,
                      after={"source": "stored", "revision": rev.number, "root_sha256": rev.root_sha256},
                      workspace=home, summary=f"{_who(request, grant)} created stored agent {agent.slug}")
    creator = creation.creating_agent(request)
    if creator is not None:
        creation.creator_administers(agent, creator)
    return Status(201, StoredAgentOut(slug=agent.slug, name=agent.name, workspace=agent.workspace_id,
                                      revision=rev.number, root_sha256=rev.root_sha256,
                                      owner=getattr(agent.owner, "email", "") or "", acted_by=_acted_by(grant),
                                      dropped=dropped))


def _decode(b64: str, path: str) -> bytes:
    try:
        return base64.b64decode(b64, validate=True)
    except (binascii.Error, ValueError):
        raise ProblemError(422, f"{path}: content_b64 is not valid base64", type_=TYPE_VALIDATION) from None


@router.post("/{slug}/definition/convert", response={201: DefinitionRevisionSummaryOut, **refusals(409, 413, 422)},
             summary="Turn a repo agent into a stored one, from its files")
@gate("agent-admin", setting=True)  # its owner or an admin, as a person: never an agent login or a turn's token
def convert_agent_definition(request: HttpRequest, slug: str, payload: DefinitionConvertIn) -> Status:
    """The uploaded files (a repo's tracked files) become revision 1, published, and the agent
    stops pointing at a repository (`repo_url` becomes ""). The file policy applies to every file:
    one outside it refuses the whole conversion (422 with `problems`). The files canopy writes
    itself (`.claude-plugin/`, `.claude/settings.json`, `bin/<slug>-email`, `.gitignore`) are
    ignored if uploaded and rewritten. 409 for an agent that is already stored.

    `bundle_b64` (a `git bundle` of the branch) keeps the repository's history: it is kept
    under `refs/heads/imported` and revision 1 is a commit on its tip. Every blob in it is
    scanned for secrets first (422 `secret` with `problems`). Files and history together are
    held to one request's size: more is 413 — convert the files only."""
    agent = agent_for_admin(request, slug)
    grant = _require_person(request, agent, "Converting an agent")
    files: dict[str, tuple[bytes, bool]] = {}
    for f in payload.files:
        if f.path in files:
            raise ProblemError(422, f"{f.path} is listed twice", type_=TYPE_VALIDATION)
        files[f.path] = (_decode(f.content_b64, f.path), f.executable)
    bundle = _decode(payload.bundle_b64, "bundle_b64") if payload.bundle_b64 else None
    before = {"source": agent.source, "repo_url": agent.repo_url, "repo_ref": agent.repo_ref}
    try:
        rev = services.convert(agent, files=files, message=payload.message, by=request.user, bundle=bundle,
                               act_as=grant)
    except services.Refused as exc:
        raise _problem(exc) from None
    audit.note_change(scope=f"agent:{agent.slug}/definition", before=before,
                      after={"source": "stored", "revision": rev.number, "root_sha256": rev.root_sha256})
    return Status(201, _summary(rev))


@router.post("/{slug}/definition/export", response={200: DefinitionMovedOut, **refusals(409, 422, 502)},
             summary="Move a stored agent's definition, with its history, to an empty GitHub repository")
# its owner or an admin, as a person: never an agent login or a turn's token
@gate("agent-admin", setting=True,
      step_up="the person's GitHub token is used for this one push and never kept; the canopy CLI runs it with a PAT")
def export_agent_definition(request: HttpRequest, slug: str, payload: DefinitionExportIn) -> DefinitionMovedOut:
    """Pushes `main` — every commit and every `r<N>` tag — to the EMPTY GitHub repository `repo`
    with `github_token` (the exporting person's, used for this push only), then switches the
    agent to `source=repo` there: `repo_url=https://github.com/<repo>`, `repo_ref=ref`, no live
    revision. Its runners clone it from GitHub from then on. Without `github_token`, 422 `no_token`:
    canopy cannot open the agent's delegated token. canopy's repository stays,
    read-only: every later draft or publish is 409 `exported`. 409 `not_empty` when the GitHub
    repository has any ref; 502 when it cannot be reached or refuses the push. Create the
    repository first."""
    agent = agent_for_admin(request, slug)
    _stored_or_409(agent)
    grant = _require_person(request, agent, "Exporting a definition")
    before = {"source": agent.source, "repo_url": agent.repo_url, "repo_ref": agent.repo_ref}
    try:
        out = services.export(agent, repo=payload.repo, github_token=payload.github_token, ref=payload.ref,
                              by=request.user)
    except services.Refused as exc:
        raise _problem(exc) from None
    audit.note_change(scope=f"agent:{agent.slug}/definition", before=before,
                      after={"source": "repo", "repo_url": out["repo_url"], "repo_ref": out["repo_ref"],
                             "exported_from_revision": out["revision"]})
    return DefinitionMovedOut(**out, acted_by=_acted_by(grant))


@router.post("/{slug}/definition/detach", response={200: DefinitionMovedOut, **refusals(409, 422)},
             summary="Point a stored agent at the GitHub repository its definition was pushed to")
@gate("agent-admin", setting=True)  # its owner or an admin, as a person: never an agent login or a turn's token
def detach_agent_definition(request: HttpRequest, slug: str, payload: DefinitionDetachIn) -> DefinitionMovedOut:
    """For a person who pushed the definition to GitHub themselves (`canopy agent definition
    export --push-myself`): the same switch as `export`, with nothing pushed by canopy. The
    agent runs from `repo_url` @ `ref` from then on and its canopy repository is a read-only
    archive."""
    agent = agent_for_admin(request, slug)
    _stored_or_409(agent)
    grant = _require_person(request, agent, "Detaching a definition")
    before = {"source": agent.source, "repo_url": agent.repo_url, "repo_ref": agent.repo_ref}
    try:
        out = services.detach(agent, repo_url=payload.repo_url, ref=payload.ref, by=request.user)
    except services.Refused as exc:
        raise _problem(exc) from None
    audit.note_change(scope=f"agent:{agent.slug}/definition", before=before,
                      after={"source": "repo", "repo_url": out["repo_url"], "repo_ref": out["repo_ref"],
                             "detached_at_revision": out["revision"]})
    return DefinitionMovedOut(**out, acted_by=_acted_by(grant))


# ---- reading -------------------------------------------------------------------


@router.get("/{slug}/definition", response=DefinitionOut, summary="An agent's stored definition: live, draft, policy")
@gate("member")
def get_agent_definition(request: HttpRequest, slug: str) -> DefinitionOut:
    """`source` says where the definition lives. For a stored agent, the live revision, the one
    draft (if any) and the file policy every write is held to. `can_draft` / `can_publish` say
    what the caller may do here: `can_publish` is a person who administers the agent, or
    another agent that does, signed in as itself."""
    agent = agent_for_read(request, slug)
    publisher = (not isinstance(acting_person.person_act_as_or_admin_agent(request, agent), str)
                 and not agent.is_own_login(request.user))
    return DefinitionOut(
        source=agent.source, git_url=store.git_url(agent) if store.has_repository(agent) else None,
        exported_to=agent.definition_exported_to,
        live=_summary(agent.live_revision), draft=_summary(services.draft(agent)),
        policy=DefinitionPolicyOut(**policy.policy()),
        can_draft=agent.is_stored and not agent.definition_exported_to and access.may_edit_agent(request.user, agent),
        can_publish=agent.is_stored and not agent.definition_exported_to and publisher and agent.is_admin(request.user))


@router.get("/{slug}/definition/revisions", response=list[DefinitionRevisionSummaryOut],
            summary="Every revision of a stored agent's definition, newest first")
@gate("member")
def list_agent_definition_revisions(request: HttpRequest, slug: str) -> list[DefinitionRevisionSummaryOut]:
    agent = agent_for_read(request, slug)
    rows = (AgentDefinitionRevision.objects.filter(agent=agent)
            .select_related(*_REVISION_RELATED).order_by("-number"))
    return [_summary(r) for r in rows]


@router.get("/{slug}/definition/revisions/{number}", response=DefinitionRevisionOut,
            summary="One revision of a stored agent's definition, with its file tree")
@gate("member")
def get_agent_definition_revision(request: HttpRequest, slug: str, number: int) -> DefinitionRevisionOut:
    """`tree` is `{path: {sha256, size, executable}}`; `root_sha256` is its manifest root
    (`canopy_sdk.agent_definition.manifest_root`)."""
    agent = agent_for_read(request, slug)
    rev = _revision_or_404(agent, number)
    return DefinitionRevisionOut(**_summary(rev).model_dump(), tree=rev.tree or {})


@router.get("/{slug}/definition/revisions/{number}/files/{path:path}",
            summary="One file of a revision, as it is stored")
@gate("member")
def get_agent_definition_file(request: HttpRequest, slug: str, number: int, path: str) -> HttpResponse:
    """The file's bytes as `text/plain; charset=utf-8` (every stored file is UTF-8 text). 404
    for a path the revision does not hold."""
    agent = agent_for_read(request, slug)
    rev = _revision_or_404(agent, number)
    data = store.read_file(rev, path)
    if data is None:
        raise ProblemError(404, f"revision {number} has no file {path}", type_=TYPE_NOT_FOUND)
    response = HttpResponse(data, content_type="text/plain; charset=utf-8")
    response["X-Content-Type-Options"] = "nosniff"
    response["Content-Security-Policy"] = "sandbox"
    return response


@router.get("/{slug}/definition/revisions/{number}/diff", response=DefinitionDiffOut,
            summary="What changed in a revision, file by file")
@gate("member")
def diff_agent_definition_revisions(request: HttpRequest, slug: str, number: int,
                                    against: int | None = None) -> DefinitionDiffOut:
    """A unified diff per changed file, from `against` to `number`. Without `against`, the
    revision is compared with the one it was made on (its parent), or with nothing."""
    agent = agent_for_read(request, slug)
    rev = _revision_or_404(agent, number)
    if against is not None:
        base = _revision_or_404(agent, against)
    elif rev.parent_id is not None:
        base = rev.parent
    else:
        base = AgentDefinitionRevision(agent=agent, number=0, tree={})
    return DefinitionDiffOut(revision=rev.number, against=base.number, files=services.diff(rev, base))


# ---- changing ------------------------------------------------------------------


@router.post("/{slug}/definition/drafts", response={201: DefinitionRevisionSummaryOut, **refusals(409, 422)},
             summary="Propose changes to a stored agent's definition (creates or replaces its one draft)")
# agent_edit on the agent, which its own login holds; an agent's draft asks its admins to publish
@gate("agent-edit", "agent-login", kind="request")
def draft_agent_definition(request: HttpRequest, slug: str, payload: DefinitionDraftIn) -> Status:
    """`changes` are applied to the LIVE revision `base`: `content` (text) or `content_b64`
    writes a file, `content: null` deletes one. The result replaces any earlier draft (whose
    approval request, if any, is withdrawn). A `base` that is not live is 409 `stale_base`; a
    change the file policy refuses is 422 with `problems`. Nothing goes live: whoever may
    publish it does (`publish`). When an agent writes it (its own login, or any turn's token),
    canopy files an approval request (`approval_id`) to the agent's owner and admins, who
    approve it from its page with a passkey, unless that agent may publish it itself:
    another agent that administers this one, signed in as itself, asks for no approval."""
    agent = agent_for_edit(request, slug)
    _stored_or_409(agent)
    by_agent = acting_person.refusal(request) is not None or agent.is_own_login(request.user)
    admin_agent = acting_person.admin_agent(request, agent) if by_agent else None
    changes = []
    for c in payload.changes:
        if c.content is not None and c.content_b64 is not None:
            raise ProblemError(422, f"{c.path}: send content or content_b64, not both", type_=TYPE_VALIDATION)
        data = c.content.encode("utf-8") if c.content is not None else (
            _decode(c.content_b64, c.path) if c.content_b64 is not None else None)
        changes.append({"path": c.path, "content": data, "executable": c.executable})
    try:
        rev = services.create_draft(agent, base=payload.base, changes=changes, message=payload.message,
                                    by=request.user, by_agent=by_agent,
                                    turn=_authoring_turn(request) if by_agent else None,
                                    asks_approval=by_agent and isinstance(admin_agent, str))
    except services.Refused as exc:
        raise _problem(exc) from None
    rev = _revision_or_404(agent, rev.number)
    return Status(201, _summary(rev))


@router.post("/{slug}/definition/revisions/{number}/publish", response={200: DefinitionRevisionSummaryOut,
                                                                       **refusals(409, 422)},
             summary="Publish a stored agent's draft: it becomes what its runners run")
@gate("agent-admin", setting=True)  # a person admin, an act-as agent, or another admin agent; never its own login
def publish_agent_definition(request: HttpRequest, slug: str, number: int) -> DefinitionRevisionSummaryOut:
    """Draft `number` goes live and the old live revision is superseded; the files canopy writes
    itself are rewritten for it, and the agent's skill catalog is read again from it. 409 when
    `number` is not the draft, or was drafted on a revision that is no longer live. Its owner
    or an admin publishes it, as themselves or through an agent they let act as them (an
    act-as-you permission, read + write, named in `acted_by`); so may ANOTHER agent that
    administers this one, signed in as itself (named in `published_by_agent`). 403 for the
    agent's own login, a caller token, or an agent that is not its admin. An approval the
    draft waited on is closed as superseded."""
    agent = agent_for_admin(request, slug)
    _stored_or_409(agent)
    grant, acting = _require_publisher(request, agent, "Publishing a definition")
    before = agent.live_revision.number if agent.live_revision_id else None
    try:
        rev = services.publish(agent, number, by=request.user, act_as=grant, admin_agent=acting)
    except services.Refused as exc:
        raise _problem(exc) from None
    audit.note_change(scope=f"agent:{agent.slug}/definition", before={"revision": before},
                      after={"revision": rev.number, "root_sha256": rev.root_sha256})
    return _summary(_revision_or_404(agent, rev.number))


@router.post("/{slug}/definition/revisions/{number}/discard", response={200: DefinitionRevisionSummaryOut,
                                                                       **refusals(409)},
             summary="Discard a stored agent's draft")
@gate("agent-edit", setting=True)
def discard_agent_definition(request: HttpRequest, slug: str, number: int) -> DefinitionRevisionSummaryOut:
    """The draft is kept, marked discarded, and its approval request (if any) is withdrawn."""
    agent = agent_for_edit(request, slug)
    _stored_or_409(agent)
    try:
        rev = services.discard(agent, number, by=request.user)
    except services.Refused as exc:
        raise _problem(exc) from None
    audit.note_change(scope=f"agent:{agent.slug}/definition/draft", before={"draft": number}, after=None)
    return _summary(_revision_or_404(agent, rev.number))


@router.post("/{slug}/definition/rollback", response={201: DefinitionRevisionSummaryOut, **refusals(409, 422)},
             summary="Roll a stored agent back to an earlier revision")
@gate("agent-admin", setting=True)  # a person admin, an act-as agent, or another admin agent; never its own login
def rollback_agent_definition(request: HttpRequest, slug: str, payload: DefinitionRollbackIn) -> Status:
    """A NEW revision with revision `revision`'s files is created and published; history is
    never rewritten. Any open draft is discarded. 409 for a revision that was never published.
    Who may: the same as `publish`."""
    agent = agent_for_admin(request, slug)
    _stored_or_409(agent)
    grant, acting = _require_publisher(request, agent, "Rolling back a definition")
    before = agent.live_revision.number if agent.live_revision_id else None
    try:
        rev = services.rollback(agent, payload.revision, by=request.user, message=payload.message,
                                act_as=grant, admin_agent=acting)
    except services.Refused as exc:
        raise _problem(exc) from None
    audit.note_change(scope=f"agent:{agent.slug}/definition", before={"revision": before},
                      after={"revision": rev.number, "root_sha256": rev.root_sha256, "from": payload.revision})
    return Status(201, _summary(_revision_or_404(agent, rev.number)))


# ---- the archive ---------------------------------------------------------------


def _http_date(value) -> str:
    return value.strftime("%Y-%m-%dT%H:%M:%SZ")


@router.get("/{slug}/definition/archive", summary="A revision of a stored agent as a gzip'd tar")
@gate("member", "runner-holds-agent")  # a member, or a runner token whose box may hold the agent
def get_agent_definition_archive(request: HttpRequest, slug: str, revision: str = "live") -> HttpResponse:
    """`application/gzip`: a tar of the revision's files (regular files only, mode 0644 or
    0755), with `X-Canopy-Revision`, `X-Canopy-Root-Sha256`, `X-Canopy-Published-At`
    (ISO 8601, UTC) and `X-Canopy-Workspace`. `revision` is a number or `live`. A runner's own token reads it for an
    agent its box may hold, live or published revisions only; a person, any revision of an
    agent they can see. It holds no credential."""
    rtok = getattr(request, "runner_token", None)
    if rtok is not None:
        agent = Agent.objects.filter(slug=slug).first()
        if (agent is None or rtok.runner.kind == Runner.COMPUTER
                or not runner_may_hold_agent(rtok.runner, agent)):
            raise HttpError(404, f"agent '{slug}' not found")
    else:
        agent = agent_for_read(request, slug)
    _stored_or_409(agent)
    if revision == "live":
        rev = agent.live_revision
        if rev is None:
            raise ProblemError(404, f"{agent.slug} has no live revision", type_=TYPE_NOT_FOUND)
    else:
        try:
            number = int(revision)
        except ValueError:
            raise ProblemError(422, "revision is a number or 'live'", type_=TYPE_VALIDATION) from None
        rev = _revision_or_404(agent, number)
        if rtok is not None and rev.status not in (AgentDefinitionRevision.PUBLISHED,
                                                   AgentDefinitionRevision.SUPERSEDED):
            # A box runs what a person published, never a draft.
            raise ProblemError(404, f"{agent.slug} has no published revision {number}", type_=TYPE_NOT_FOUND)
    response = HttpResponse(store.archive(rev), content_type="application/gzip")
    response["Content-Disposition"] = f'attachment; filename="{agent.slug}-r{rev.number}.tar.gz"'
    response["X-Canopy-Revision"] = str(rev.number)
    response["X-Canopy-Workspace"] = agent.workspace_id
    response["X-Canopy-Root-Sha256"] = rev.root_sha256
    response["X-Canopy-Published-At"] = _http_date(rev.published_at or rev.created_at)
    response["Cache-Control"] = "private, no-store"
    return response

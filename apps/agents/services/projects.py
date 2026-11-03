from __future__ import annotations

import re

from django.db import IntegrityError, transaction
from django.db.models import F, Q
from django.utils.text import slugify

from apps.workspaces import services as wsvc
from apps.workspaces.models import Workspace

from .. import participants, work
from ..models import Agent, AgentProject, ProjectShare


def agent_projects_q(agent: Agent):
    """The projects an agent's board shows: the ones it leads or works on."""
    return Q(agent=agent) | Q(member_agents=agent)


def list_projects(agent: Agent, status: str = "") -> list:
    qs = (AgentProject.objects.filter(agent_projects_q(agent)).distinct()
          .select_related("agent", "owner_user", "workspace"))
    if status:
        qs = qs.filter(status__in=[s.strip() for s in status.split(",") if s.strip()])
    return list(qs)


def get_project(agent: Agent, ref: str):
    """A project an agent names: by its alias ("P3", its own first, then one a
    project it leads or works on carries), by numeric id among those, or by
    handle ("givewell-kb") or address ("connect/givewell-kb"). A bare handle
    is looked up in the agent's workspace, then up its tree (a division's
    agents use the org's projects); an address may also name a workspace below
    the agent's, where an agent of the parent can lead a project."""
    ref = str(ref or "").strip()
    if not ref:
        return None
    base = AgentProject.objects.select_related("agent", "owner_user", "workspace")
    project = base.filter(created_by_agent=agent, ext_id__iexact=ref).first()
    if project is None:
        project = base.filter(agent_projects_q(agent), ext_id__iexact=ref).distinct().first()
    if project is None and ref.isdigit():
        project = base.filter(agent_projects_q(agent), pk=int(ref)).distinct().first()
    if project is None:
        ws, _, handle = ref.rpartition("/")
        lineage = [agent.workspace_id, *agent.workspace.ancestor_slugs()]
        if ws:
            if ws in lineage or may_hold_workspace_project(agent, ws):
                project = get_workspace_project(ws, handle)
            else:
                # a project of another workspace of the tree, shared with the agent's
                shared = get_workspace_project(ws, handle)
                if shared is not None and may_join_workspace_project(agent, shared):
                    project = shared
        else:
            for slug in lineage:
                project = get_workspace_project(slug, handle)
                if project is not None:
                    break
    return project


def may_hold_workspace_project(steward: Agent, workspace_slug: str) -> bool:
    """May `steward` lead (or be assigned work in) `workspace_slug`? Its own
    workspace, or one below it: an org's agent can curate a division's
    resource (Eva, of dimagi, leads `connect/givewell-kb`), but not the other
    way round, nor across organisations."""

    if workspace_slug == steward.workspace_id:
        return True
    ws = Workspace.objects.filter(slug=workspace_slug).first()
    return ws is not None and steward.workspace_id in ws.ancestor_slugs()


# ---- sharing a project across its workspace tree ----
#
# A project belongs to one workspace. Sharing it with another workspace of the
# SAME tree (`ProjectShare`) opens it to that workspace's members and agents and
# to the ones below: its members may read it, and its agents may join it and be
# assigned its tasks. Leading it stays with the workspace's own side
# (`may_hold_workspace_project`). Nothing is ever shared across organisations.


def project_share_slugs(project: AgentProject) -> set[str]:
    """The workspaces `project` is shared with (not the ones below them)."""
    if project.pk is None:
        return set()
    return {sh.workspace_id for sh in project.shares.all()}


def project_reader_slugs(project: AgentProject) -> set[str]:
    """Every workspace whose members may read `project`: its own, its lead
    agent's, and the shared ones with everything below them."""
    out = {project.workspace_id}
    if project.agent_id:
        out.add(project.agent.workspace_id)
    shares = project_share_slugs(project)
    if shares:
        out |= shares | wsvc.descendant_slugs(shares)
    return out


def shared_with_q(slugs, prefix: str = ""):
    """Projects shared with a workspace in `slugs` or above one of them (a share
    covers the workspaces below it): the rows `slugs`' members may read through
    a share. `prefix` reaches the project from another model."""
    return Q(**{f"{prefix}shares__workspace_id__in": list(wsvc.with_ancestors(slugs))})


def may_join_workspace_project(agent: Agent, project: AgentProject) -> bool:
    """May `agent` work on `project` (be a member, be assigned its tasks)? The
    rule for leading it (`may_hold_workspace_project`), or: the project is shared
    with the agent's workspace or one above it, in the project's own tree."""
    if may_hold_workspace_project(agent, project.workspace_id):
        return True
    shares = project_share_slugs(project)
    if not shares:
        return False
    home = {agent.workspace_id, *agent.workspace.ancestor_slugs()}
    if not (home & shares):
        return False
    # the hard invariant, asked again here rather than trusted from write time
    roots = wsvc.tree_roots({agent.workspace_id, project.workspace_id})
    return roots[agent.workspace_id] == roots[project.workspace_id]


def mark_shared_in(projects: list, slugs) -> list:
    """Flag the rows of a list that `slugs` reach only through a share."""
    own = set(slugs)
    for p in projects:
        p._shared_in = not (p.workspace_id in own or (p.agent_id and p.agent.workspace_id in own))
    return projects


def share_project(project: AgentProject, workspace_slug: str, *, by=None) -> ProjectShare:
    """Share `project` with a workspace of its tree (and those below it).
    422 unless that workspace is in the project's own organisation and is
    neither the project's workspace nor one above it (those reach it already).
    Sharing again is a no-op."""
    slug = (workspace_slug or "").strip()
    ws = Workspace.objects.filter(slug=slug).first()
    if ws is None:
        raise ProjectScopeError(f"no workspace '{slug}'", 404)
    roots = wsvc.tree_roots({ws.slug, project.workspace_id})
    if roots[ws.slug] != roots[project.workspace_id]:
        raise ProjectScopeError(f"'{slug}' is not in the same organisation as '{project.workspace_id}': "
                                "a project is never shared across organisations")
    if slug == project.workspace_id or slug in project.workspace.ancestor_slugs():
        raise ProjectScopeError(f"'{slug}' already reaches '{project.address}': it is the project's "
                                "workspace or above it")
    share, _ = ProjectShare.objects.get_or_create(
        project=project, workspace=ws,
        defaults={"added_by": by if getattr(by, "is_authenticated", False) else None})
    return share


def members_through_share(project: AgentProject, share_slug: str) -> list[Agent]:
    """The project's member agents (never the lead) that belong to `share_slug`
    or a workspace below it and are members only because of a share."""
    out = []
    below = {share_slug} | wsvc.descendant_slugs({share_slug})
    for a in project.member_agents.select_related("workspace"):
        if a.pk != project.agent_id and a.workspace_id in below \
                and not may_hold_workspace_project(a, project.workspace_id):
            out.append(a)
    return out


def unshare_project(project: AgentProject, workspace_slug: str) -> list[str]:
    """End a share. Member agents who no longer qualify are removed (never the
    lead agent); returns their slugs."""
    slug = (workspace_slug or "").strip()
    share = project.shares.filter(workspace_id=slug).first()
    if share is None:
        raise ProjectScopeError(f"'{project.address}' is not shared with '{slug}'", 404)
    with transaction.atomic():
        share.delete()
        project.__dict__.pop("_prefetched_objects_cache", None)
        removed = [a for a in project.member_agents.select_related("workspace")
                   if a.pk != project.agent_id and not may_join_workspace_project(a, project)]
        if removed:
            project.member_agents.remove(*removed)
    return sorted(a.slug for a in removed)


def prune_cross_tree_shares(workspace: Workspace) -> int:
    """After `workspace` (and so its subtree) changed parent: delete the shares
    that now straddle two organisations. Returns how many."""
    if not ProjectShare.objects.exists():
        return 0
    affected = {workspace.slug} | wsvc.descendant_slugs({workspace.slug})
    rows = list(ProjectShare.objects.filter(Q(workspace_id__in=affected) | Q(project__workspace_id__in=affected))
                .select_related("project"))
    if not rows:
        return 0
    roots = wsvc.tree_roots({r.workspace_id for r in rows} | {r.project.workspace_id for r in rows})
    gone = [r.pk for r in rows if roots[r.workspace_id] != roots[r.project.workspace_id]]
    ProjectShare.objects.filter(pk__in=gone).delete()
    return len(gone)


class ProjectScopeError(ValueError):
    """A project write that cannot be done, with its HTTP status."""

    def __init__(self, message: str, status: int = 422):
        super().__init__(message)
        self.status = status


def get_workspace_project(workspace_slug: str, handle: str):
    return (AgentProject.objects.select_related("agent", "owner_user", "workspace")
            .filter(workspace_id=workspace_slug, handle__iexact=(handle or "").strip()).first())


def workspace_projects(workspace_slugs, status: str = "", steward_workspaces=()) -> list:
    """Owned by any of `workspace_slugs` — or led from any of
    `steward_workspaces` (a parent's agent leading a division's project) — or
    shared with one of them (or an ancestor of one: a share covers what is below
    it); those carry `shared_in`."""
    workspace_slugs, steward_workspaces = set(workspace_slugs), set(steward_workspaces)
    qs = (AgentProject.objects.select_related("agent", "owner_user", "workspace").prefetch_related("shares")
          .filter(Q(workspace_id__in=list(workspace_slugs))
                  | Q(agent__workspace_id__in=list(steward_workspaces))
                  | shared_with_q(workspace_slugs)).distinct())
    if status:
        qs = qs.filter(status__in=[s.strip() for s in status.split(",") if s.strip()])
    return mark_shared_in(list(qs.order_by("workspace_id", "handle")),
                          workspace_slugs | steward_workspaces)


def _clean_handle(handle: str) -> str:
    handle = (handle or "").strip().lower()
    if not re.match(AgentProject.HANDLE_RE, handle):
        raise ProjectScopeError("handle must be 3-60 characters of a-z, 0-9 and '-', "
                                "starting and ending with a letter or digit")
    return handle


def free_handle(workspace_slug: str, name: str, fallback: str = "project") -> str:
    """A handle for a new project from its name, unique in the workspace."""
    base = slugify(name or "")[:56].strip("-")
    if not re.match(AgentProject.HANDLE_RE, base):
        base = slugify(fallback)[:56].strip("-")
    if not re.match(AgentProject.HANDLE_RE, base):
        base = "project"
    taken = set(AgentProject.objects.filter(workspace_id=workspace_slug, handle__startswith=base[:52])
                .values_list("handle", flat=True))
    handle, i = base, 2
    while handle in taken:
        handle = f"{base[:52]}-{i}"
        i += 1
    return handle


def promote_project(project: AgentProject, handle: str, workspace: str = "") -> AgentProject:
    """Give a project the address `<workspace>/<handle>` — a new handle, and/or
    a workspace below its lead agent's. The same address is a no-op; an address
    already taken is refused (409). Its alias, tasks, folder, history and its
    knowledge base's stored objects (`kb_store.root`, not keyed by the address)
    are untouched."""
    handle = _clean_handle(handle)
    workspace = (workspace or "").strip() or project.workspace_id
    if project.handle == handle and project.workspace_id == workspace:
        return project
    if project.agent_id is not None and not may_hold_workspace_project(project.agent, workspace):
        raise ProjectScopeError(f"'{project.agent.slug}' can only lead a project of "
                                f"'{project.agent.workspace_id}' or a workspace below it")
    project.workspace_id = workspace
    project.handle = handle
    try:
        with transaction.atomic():
            project.save(update_fields=["workspace", "handle", "updated_at"])
    except IntegrityError:
        raise ProjectScopeError(f"'{workspace}/{handle}' is already taken", 409) from None
    # A task lives where its project does.
    for task in project.tasks.exclude(workspace_id=workspace):
        work.rehome(task, workspace)
    _settle_shares(project)
    _settle_vault_attachments(project)
    return project


def _settle_vault_attachments(project: AgentProject) -> None:
    """After a project changed workspace: a workspace vault serves its members only while the
    project's workspace lies in the vault's tree, so an attachment the new home no longer
    satisfies is dropped (the same rule `workspace_vaults.attach` enforces)."""
    for att in project.vault_attachments.select_related("vault"):
        if project.workspace_id not in {att.vault.workspace_id} | wsvc.descendant_slugs({att.vault.workspace_id}):
            att.delete()


def _settle_shares(project: AgentProject) -> None:
    """After a project changed workspace: drop the shares that no longer make
    sense there (another organisation, or the project's own workspace or one above
    it), then the member agents that no longer qualify — never the lead agent."""
    project.__dict__.pop("_prefetched_objects_cache", None)
    project.refresh_from_db(fields=["workspace"])
    above = {project.workspace_id, *project.workspace.ancestor_slugs()}
    shares = list(project.shares.all())
    if shares:
        roots = wsvc.tree_roots({sh.workspace_id for sh in shares} | {project.workspace_id})
        stale = [sh.pk for sh in shares
                 if sh.workspace_id in above or roots[sh.workspace_id] != roots[project.workspace_id]]
        ProjectShare.objects.filter(pk__in=stale).delete()
    drop = [a for a in project.member_agents.select_related("workspace")
            if a.pk != project.agent_id and not may_join_workspace_project(a, project)]
    if drop:
        project.member_agents.remove(*drop)


def set_steward(project: AgentProject, steward: Agent | None) -> AgentProject:
    """Make `steward` the project's lead agent (None: no agent leads it). It
    must belong to the project's workspace or one above it. The address is the
    identity, so callers notice nothing; the alias stays its maker's."""
    if steward is None:
        project.agent = None
        project.save(update_fields=["agent", "updated_at"])
        return project
    if not may_hold_workspace_project(steward, project.workspace_id):
        raise ProjectScopeError(f"the lead agent must be an agent of '{project.workspace_id}' "
                                "or of a workspace above it")
    if steward.pk != project.agent_id:
        project.agent = steward
        project.save(update_fields=["agent", "updated_at"])
    project.member_agents.add(steward)
    return project


def set_member_agents(project: AgentProject, agents: list[Agent]) -> AgentProject:
    """Replace the agents working on it. Its lead agent stays one."""
    for a in agents:
        if not may_join_workspace_project(a, project):
            raise ProjectScopeError(f"'{a.slug}' is not an agent of '{project.workspace_id}', of a "
                                    "workspace above it, or of one the project is shared with")
    keep = {a.pk for a in agents}
    if project.agent_id:
        keep.add(project.agent_id)
    project.member_agents.set(list(keep))
    return project


def stewarded_workspace_projects(agent: Agent) -> list[str]:
    """Addresses of the projects `agent` leads OUTSIDE its own workspace (a
    division's, led from the org) — what blocks moving it: the move would take
    it out of the tree those projects live in."""
    return [p.address for p in agent.projects.exclude(workspace_id=agent.workspace_id)]


def next_project_ext_id(agent: Agent) -> str:
    """P1, P2, … — from a counter that only ever goes up.

    Not from the highest existing project: deleting P2 would hand "P2" out
    again, and an old link — a task's Links, a Drive folder, an email — would
    then point at different work. The increment is a single UPDATE so two turns
    numbering at once cannot collide.
    """
    Agent.objects.filter(pk=agent.pk).update(project_seq=F("project_seq") + 1)
    agent.refresh_from_db(fields=["project_seq"])
    return f"P{agent.project_seq}"


_PROJECT_FIELDS = ("name", "outcome", "status", "owner_note", "drive_folder_id",
                   "drive_folder_url", "repo_slug", "notes", "priority", "start_date", "target_date")


_PROJECT_STATUS = {s for s, _ in AgentProject.STATUS_CHOICES}


def _norm_project_status(value: str) -> str:
    return value if value in _PROJECT_STATUS else AgentProject.ACTIVE


def new_project(*, workspace: str, name: str, agent: Agent | None = None, by_agent: Agent | None = None,
                by_user=None, handle: str = "", ext_id: str = "", **fields) -> AgentProject:
    """The one constructor of a project row: its workspace, a handle (given, or
    free from its name), its lead agent as a member, and — made through an
    agent's route — that agent's alias."""
    if agent is not None and not may_hold_workspace_project(agent, workspace):
        raise ProjectScopeError(f"'{agent.slug}' can only lead a project of "
                                f"'{agent.workspace_id}' or a workspace below it")
    handle = _clean_handle(handle) if handle else free_handle(workspace, name, fallback=ext_id or "project")
    try:
        with transaction.atomic():
            project = AgentProject.objects.create(
                workspace_id=workspace, handle=handle, agent=agent, name=name,
                created_by_agent=by_agent, ext_id=ext_id,
                created_by_user=by_user if getattr(by_user, "is_authenticated", False) else None,
                **fields)
    except IntegrityError:
        raise ProjectScopeError(f"'{workspace}/{handle}' is already taken", 409) from None
    if agent is not None:
        project.member_agents.add(agent)
    return project


def create_project(agent: Agent | None, data, *, workspace: str = "", by_user=None) -> AgentProject:
    """A project from an API body. Through an agent's route (`agent` given and
    no `workspace`), the agent leads it, it lives in the agent's workspace and
    carries the agent's next `P<n>` alias. Otherwise it is made in `workspace`,
    led by `agent` if one is named."""
    payload = {f: getattr(data, f) for f in _PROJECT_FIELDS if getattr(data, f, None) is not None}
    payload["status"] = _norm_project_status(payload.get("status", AgentProject.ACTIVE))
    if getattr(data, "links", None):
        payload["links"] = [link.model_dump() for link in data.links]
    via_agent = agent is not None and not workspace
    ws = workspace or agent.workspace_id
    owner_email = getattr(data, "owner_email", "") or getattr(data, "lead_email", "")
    payload["owner_user"] = resolve_project_owner_in(ws, owner_email)
    name = payload.pop("name")
    ext_id = ""
    if via_agent:
        ext_id = (getattr(data, "ext_id", "") or "").strip() or next_project_ext_id(agent)
    return new_project(workspace=ws, name=name, agent=agent, by_agent=agent if via_agent else None,
                       by_user=by_user, handle=(getattr(data, "handle", "") or "").strip(),
                       ext_id=ext_id, **payload)


def patch_project(project: AgentProject, data: dict) -> AgentProject:
    if "status" in data and data["status"] is not None:
        new = _norm_project_status(data["status"])
        if (project.status == AgentProject.PROPOSED and new not in
                (AgentProject.PROPOSED, AgentProject.CANCELED, AgentProject.ARCHIVED)):
            raise ProjectScopeError("a proposed project is started through its decision card "
                                    "(approve its decision task), not by changing its status", 409)
    for f in _PROJECT_FIELDS:
        if f in data:
            value = data[f]
            if f == "status":
                value = _norm_project_status(value)
            elif f == "priority":
                value = int(value or 0)
                if not 0 <= value <= 4:
                    raise ValueError("priority is 0 (none) to 4 (low)")
            setattr(project, f, value)
    if "links" in data:
        project.links = data["links"]
    for key in ("owner_email", "lead_email"):
        if key in data:
            project.owner_user = resolve_project_owner_in(project.workspace_id, data[key])
    project.save()
    return project


def set_task_project(task, project) -> None:
    """Put a task in a project (or take it out with None). It moves to the
    project's workspace."""
    task.project = project
    task.save(update_fields=["project", "updated_at"])
    if project is not None and project.workspace_id != task.workspace_id:
        work.rehome(task, project.workspace_id)
    _link_participants(task)


def _link_participants(task) -> None:
    """A task in a project raised by a human's turn makes them a participant
    (fleet brain v1.1). Best-effort: never fails the task write."""

    participants.on_task(task)


class UnknownPersonError(Exception):
    """canopy cannot route a wait to somebody it has never seen."""


def resolve_project_owner_in(workspace_slug: str, email: str):
    return _workspace_member(workspace_slug, email, instead="owner_note")


def _workspace_member(workspace_slug: str, email: str, *, instead: str):
    email = (email or "").strip().lower()
    if not email:
        return None
    from django.contrib.auth import get_user_model

    user = get_user_model().objects.filter(email__iexact=email).first()
    if user is None or not wsvc.is_member(user, workspace_slug):
        raise UnknownPersonError(
            f"{email or 'that person'} is not a member of this workspace — "
            f"put the name in `{instead}` instead"
        )
    return user

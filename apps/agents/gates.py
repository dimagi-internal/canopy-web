"""Resolve an agent by slug for THIS request, gated by tenancy — else 404.

The one implementation every API module asks (agents, harness, agent_runs,
capabilities, threads, mail, huddles…). It is one shared copy, not one per
api module ("api modules must not depend on each other"), because copies drift:
one could honour the `/api/w/{ws}/` pin as exactly that workspace while
`wsvc.member_scope` (the by-id rule) uses the pin's subtree, or let the agent's
own login in without membership. This module is not an api
module — it holds no routes — so importing it couples nobody to anyone's
router.

Three tiers, resolve-then-authorize, so a caller outside the tenant always gets
the same 404 as a missing agent and never a 403 (no existence leak):

* `agent_for_read`  — membership: may see and talk to the agent.
* `agent_for_work`  — `perms.AGENT_WORK` (editor and up): may reshape or drive it.
* `agent_for_edit`  — `access.may_edit_agent` (`agent_edit` on this agent): may change the agent itself.
* `agent_for_admin` — `Agent.is_admin`: may change its secrets / identity.

Who may make an agent DO what is `apps.agents.turn_context.resolve_turn_context`, not this module;
these gates answer only "may this caller reach this agent's API at all".
"""
from __future__ import annotations

from django.http import HttpRequest
from ninja.errors import HttpError

from apps.workspaces import permissions as perms
from apps.workspaces import services as wsvc

from . import project_members, services
from .models import Agent


def _not_found(slug: str) -> HttpError:
    return HttpError(404, f"agent '{slug}' not found")


def agent_for_read(request: HttpRequest, slug: str, *, honor_pin: bool = True) -> Agent:
    """The agent, if it lives in a workspace this request may reach by id; else 404.

    `honor_pin=True` (every route but one): `wsvc.request_member_slugs` — pinned,
    the pin and the caller's workspaces below it; unpinned, every membership.
    `honor_pin=False`: every membership whatever the URL pin — for naming the
    OTHER agents of a cross-workspace object (a thread's participants live in
    several workspaces of its tree), where the pin names the object's home, not
    each participant's.

    The agent's own canopy login is a caller like any other here: it reaches
    the agent only as a member of the agent's workspace. Making the login, or
    moving the agent, makes it one (`agents.agent_login`); a workspace admin who
    removes it means it, so there is no own-login way in past membership.

    A non-member, a wrong-tenant pin and a missing slug all get the same 404. A
    workspace-less agent is unrepresentable (`Agent.workspace` is NOT NULL) and
    would match no scope anyway — it fails closed, never open.
    """
    agent = Agent.objects.filter(slug=slug).first()
    if agent is None or not agent.workspace_id:
        raise _not_found(slug)
    scope = (wsvc.request_member_slugs(request) if honor_pin
             else wsvc.member_scope(getattr(request, "user", None)))
    if agent.workspace_id not in scope:
        raise _not_found(slug)  # wrong tenant / non-member
    return agent


def agent_for_work(
    request: HttpRequest,
    slug: str,
    *,
    allow_own_login: bool = False,
    honor_pin: bool = True,
    forbidden: str = "this action requires the editor role or above",
) -> Agent:
    """An agent the caller may RESHAPE or DRIVE: `perms.AGENT_WORK` in its workspace.

    `agent_for_read` runs first, so a non-member gets 404, never 403.

    `allow_own_login=True` lets the agent's own canopy login stand in for the
    editor role — the agent writing its own runs, leasing its own capabilities,
    under whatever role a turn's cap left that login with. It is a member all
    the same: membership is `agent_for_read`'s, and has no exception.

    `forbidden` is the 403 detail, so each route keeps telling the caller what
    it was they could not do.
    """
    agent = agent_for_read(request, slug, honor_pin=honor_pin)
    if allow_own_login and agent.is_own_login(request.user):
        return agent
    if not perms.can(request.user, agent.workspace_id, perms.AGENT_WORK):
        raise HttpError(403, forbidden)
    return agent


def agent_for_edit(request: HttpRequest, slug: str, *, honor_pin: bool = True) -> Agent:
    """An agent the caller may CHANGE ITSELF (its skills, persona, repo work): `agent_edit` on it.

    `agent_for_read` runs first, so a non-member gets 404, never 403. The 403 names who can
    give the caller the editor role (`access.edit_refusal`).
    """
    from . import access  # noqa: PLC0415 — access imports the harness, which imports this module

    agent = agent_for_read(request, slug, honor_pin=honor_pin)
    if not access.may_edit_agent(request.user, agent):
        raise HttpError(403, access.edit_refusal(agent))
    return agent


def agent_for_admin(request: HttpRequest, slug: str) -> Agent:
    """An agent whose SECRETS the caller may change: its owner or an admin.

    Credentials and the vault pointer are the keys a runner resolves everything
    else from, so writing them is equivalent to controlling the agent end to end:
    the agent's own owner or any of its admins — an explicit, per-agent grant
    (`AgentAdmin`), workspace owners included (`Agent.is_admin`).

    `agent_for_read` runs first: a non-member gets 404, never 403.
    """
    agent = agent_for_read(request, slug)
    if not agent.is_admin(request.user):
        raise HttpError(403, "this action requires the agent's owner or an admin")
    return agent


def visible_agent_workspaces(request: HttpRequest) -> set[str]:
    """The agent workspaces a BY-SLUG / BY-ID gate admits on this request —
    exactly the scope `agent_for_read` checks (`wsvc.request_member_slugs`):
    pinned, the pin and the caller's workspaces below it; unpinned, every
    membership; never refused. For a query that must agree with the gate
    (an agent's row reached through a task or project)."""
    return wsvc.request_member_slugs(request)


def listed_agent_workspaces(request: HttpRequest) -> set[str]:
    """The agent workspaces a LIST spans (`list_agents`, the fleet task and
    project lists): the read rule (`wsvc.request_workspace_slugs`) — a caller
    whose workspaces sit in more than one tenant tree and who pins none gets a
    422 rather than every organisation's agents in one list. Always a subset of
    `visible_agent_workspaces`, so a row a list shows is one the gate admits."""
    return wsvc.request_workspace_slugs(request)


def project_or_404(request: HttpRequest, agent: Agent, ref: str):
    """One of `agent`'s projects by ref, if the caller may read it; else 404. Gate
    the agent first."""

    project = services.get_project(agent, ref)
    if project is None or not services.may_read_project(request.user, project):
        raise HttpError(404, f"project {ref} not found")
    return project


# ---- tasks and projects: the workspace's, gated by the workspace ----------
#
# A task or project belongs to a workspace, so reading one
# is membership THERE (the same scope `agent_for_read` checks), and a key or
# address outside it is the same 404 as one that does not exist.


def agent_in_project_tree(project, slug: str) -> Agent:
    """A member agent named for `project`, found by the PROJECT's tree rather
    than the caller's reach (the caller is already its editor): any agent of the
    project's organisation. Another organisation's agent is the same 404 as one
    that does not exist; whether it may join is `services.set_member_agents`'s 422."""
    agent = Agent.objects.filter(slug=slug).select_related("workspace").first()
    if agent is not None:
        roots = wsvc.tree_roots({agent.workspace_id, project.workspace_id})
        if roots[agent.workspace_id] == roots[project.workspace_id]:
            return agent
    raise HttpError(404, f"agent '{slug}' not found")


def shared_with_caller(request: HttpRequest, project) -> bool:
    """Is `project` shared with a workspace the caller belongs to, or one above
    it (a share covers the workspaces below it)?"""
    shares = services.project_share_slugs(project)
    return bool(shares and shares & wsvc.with_ancestors(visible_agent_workspaces(request)))


def task_in_reach(request: HttpRequest, task) -> bool:
    """May the caller see this task's workspace — or is it in a project shared
    with a workspace they belong to? Either way, one in a project they may read."""
    if task.project_id is not None and not services.may_read_project(request.user, task.project):
        return False
    if task.workspace_id in visible_agent_workspaces(request):
        return True
    return task.project_id is not None and shared_with_caller(request, task.project)


def task_or_404(request: HttpRequest, key: str):
    """A task by key, if the caller may see its workspace; else 404."""
    from . import work  # noqa: PLC0415 — work imports models only

    task = work.task_by_key(key)
    if task is None or not task_in_reach(request, task):
        raise HttpError(404, f"task {key} not found")
    return task


def check_task_visible(request: HttpRequest, task, ref: str = "") -> None:
    """404 unless the caller may see the task's workspace — for a task found by
    a global key through an agent's route."""
    if not task_in_reach(request, task):
        raise HttpError(404, f"task {ref or task.key} not found")


def may_edit_task(request: HttpRequest, task) -> bool:
    """May the caller change this task's fields? An editor of its workspace
    (`perms.AGENT_WORK`), its assignee person, or the login of the agent it is
    assigned to or that filed it."""
    user = request.user
    if perms.can(user, task.workspace_id, perms.AGENT_WORK):
        return True
    if task.assignee_user_id is not None and task.assignee_user_id == getattr(user, "pk", None):
        return True
    return any(a is not None and a.is_own_login(user) for a in (task.agent, task.created_by_agent))


def require_task_edit(request: HttpRequest, task) -> None:
    if not may_edit_task(request, task):
        raise HttpError(403, "changing a task requires the editor role, being its assignee, or being its agent")


def project_ws_or_404(request: HttpRequest, workspace: str, handle: str):
    """A project by address, if the caller may see its workspace or its lead
    agent's (a dimagi agent leading connect's project reads it as the dimagi
    member it is), and may read it; else 404 — the same for "no such project"."""
    project = services.get_workspace_project(workspace, handle)
    # By address: this names its workspace, so it is never the read `workspace-required` refuses.
    if project is None or not project_in_reach(request, project) \
            or not services.may_read_project(request.user, project):
        raise HttpError(404, f"project {workspace}/{handle} not found")
    return project


def project_in_reach(request: HttpRequest, project) -> bool:
    """May the caller read `project`: a member of its workspace or of its lead
    agent's, or of a workspace it is shared with? (A MEMBER agent's workspace
    is not enough — reaching an agent does not open every project it works on.)
    And it is one they may read at all."""
    if not services.may_read_project(request.user, project):
        return False
    visible = visible_agent_workspaces(request)
    if project.workspace_id in visible:
        return True
    if project.agent_id is not None and project.agent.workspace_id in visible:
        return True
    return shared_with_caller(request, project)


def may_decide_publishing(request: HttpRequest, project) -> bool:
    """May the caller let `project`'s lead agent publish its own updates? An
    admin of the project's workspace — and a PERSON: an agent's login never
    grants itself (or another agent) that."""
    user = request.user
    if not perms.can(user, project.workspace_id, perms.PROJECTS_DECIDE):
        return False
    return not Agent.objects.filter(user=user).exists()


def project_agent_login(request: HttpRequest, project):
    """The project's lead or member agent whose own login is the caller, or None."""
    agents = [a for a in [project.agent, *project.member_agents.all()] if a is not None]
    return next((a for a in agents if a.is_own_login(request.user)), None)


def may_edit_project(request: HttpRequest, project) -> bool:
    """An editor of its workspace or of its lead agent's, a person on it (its lead, or
    `project_members`), or the login of an agent leading or working on it."""
    user = request.user
    if perms.can(user, project.workspace_id, perms.AGENT_WORK):
        return True
    if project.agent_id is not None and (perms.can(user, project.agent.workspace_id, perms.AGENT_WORK)
                                         or project.agent.is_own_login(user)):
        return True
    if project_members.is_member(project, user):
        return True
    return project.member_agents.filter(user=user).exists() if getattr(user, "is_authenticated", False) else False


def require_project_edit(request: HttpRequest, project) -> None:
    if not may_edit_project(request, project):
        raise HttpError(403, "changing a project requires the editor role, being on it, or being its agent")

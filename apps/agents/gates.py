"""Resolve an agent by slug for THIS request, gated by tenancy — else 404.

The one implementation every API module asks (agents, harness, agent_runs,
capabilities, threads, mail, huddles…). It used to be hand-copied in each of
them "because api modules must not depend on each other", and the copies
drifted: some honoured the `/api/w/{ws}/` pin as exactly that workspace while
`wsvc.member_scope` (the by-id rule) had moved on to the pin's subtree; one
let the agent's own login in without membership. This module is not an api
module — it holds no routes — so importing it couples nobody to anyone's
router.

Three tiers, resolve-then-authorize, so a caller outside the tenant always gets
the same 404 as a missing agent and never a 403 (no existence leak):

* `agent_for_read`  — membership: may see and talk to the agent.
* `agent_for_work`  — `perms.AGENT_WORK` (editor and up): may reshape or drive it.
* `agent_for_admin` — `Agent.is_admin`: may change its secrets / identity.

Who may make an agent DO what is `apps.agents.access.decide`, not this module;
these gates answer only "may this caller reach this agent's API at all".
"""
from __future__ import annotations

from django.http import HttpRequest
from ninja.errors import HttpError

from apps.workspaces import permissions as perms
from apps.workspaces import services as wsvc

from . import services
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
    removes it means it, so there is no own-login door past membership.

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


def project_or_404(agent: Agent, ref: str):
    """One of `agent`'s projects by ref, else 404. Gate the agent first."""

    project = services.get_project(agent, ref)
    if project is None:
        raise HttpError(404, f"project {ref} not found")
    return project


# ---- tasks and projects: the workspace's, gated by the workspace ----------
#
# A task or project belongs to a workspace (design 2026-10-24), so reading one
# is membership THERE (the same scope `agent_for_read` checks), and a key or
# address outside it is the same 404 as one that does not exist.


def task_or_404(request: HttpRequest, key: str):
    """A task by key, if the caller may see its workspace; else 404."""
    from . import work  # noqa: PLC0415 — work imports models only

    task = work.task_by_key(key)
    if task is None or task.workspace_id not in visible_agent_workspaces(request):
        raise HttpError(404, f"task {key} not found")
    return task


def check_task_visible(request: HttpRequest, task, ref: str = "") -> None:
    """404 unless the caller may see the task's workspace — for a task found by
    a global key through an agent's route."""
    if task.workspace_id not in visible_agent_workspaces(request):
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
    member it is); else 404 — the same for "no such project"."""
    project = services.get_workspace_project(workspace, handle)
    if project is None or (workspace not in set(listed_agent_workspaces(request))
                           and workspace not in visible_agent_workspaces(request)
                           and (project.agent_id is None
                                or project.agent.workspace_id not in visible_agent_workspaces(request))):
        raise HttpError(404, f"project {workspace}/{handle} not found")
    return project


def may_edit_project(request: HttpRequest, project) -> bool:
    """An editor of its workspace or of its lead agent's, its lead, or the
    login of an agent leading or working on it."""
    user = request.user
    if perms.can(user, project.workspace_id, perms.AGENT_WORK):
        return True
    if project.agent_id is not None and (perms.can(user, project.agent.workspace_id, perms.AGENT_WORK)
                                         or project.agent.is_own_login(user)):
        return True
    if project.owner_user_id is not None and project.owner_user_id == getattr(user, "pk", None):
        return True
    return project.member_agents.filter(user=user).exists() if getattr(user, "is_authenticated", False) else False


def require_project_edit(request: HttpRequest, project) -> None:
    if not may_edit_project(request, project):
        raise HttpError(403, "changing a project requires the editor role, being its lead, or being its agent")

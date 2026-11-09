"""The roles a workspace gives out, over HTTP: `/api/workspaces/{slug}/agent-roles`.

A role is a named bundle of agent permissions an access-list line can hold.
Any member reads the roles visible to a
workspace (its own and those above). A workspace admin defines and edits the ones that cannot drive or
change an agent; one that does (`write`, which `agent_edit` carries) is the workspace owner's call, as a
default access line that drives every agent is. Every write is a settings change and takes a passkey
step-up from a browser; a token is refused for a role that drives, and allowed otherwise.
"""
from __future__ import annotations

from django.db.models import Count, Q
from django.http import HttpRequest
from ninja import Router, Status
from ninja.errors import HttpError

from apps.api.route_gates import gate
from apps.events import audit
from apps.passkeys import stepup
from apps.workspaces import permissions as perms
from apps.workspaces import services as wsvc
from apps.workspaces.models import Workspace

from . import api_access_list, roleadmin, rolecatalog, roles
from .models import AgentRole
from .schemas.roles import (
    AgentRoleIn,
    AgentRoleOut,
    AgentRolePatch,
    AgentRolesOut,
    AgentRoleUseOut,
    FixedRoleOut,
)

router = Router(tags=["workspaces"])

_FIXED_WHY = {roles.OWNER: "whoever owns the agent; one person, never a line",
              roles.ADMIN: "people who hold the agent's keys (its credentials and vault); named as agent "
                           "admins, never by a line"}


def _workspace_or_404(request: HttpRequest, slug: str) -> Workspace:
    ws = Workspace.objects.filter(slug=slug).first()
    if ws is None or slug not in wsvc.request_member_slugs(request):
        raise HttpError(404, f"workspace {slug!r} not found")
    return ws


def _role_or_404(ws: Workspace, key: str) -> AgentRole:
    row = AgentRole.objects.filter(workspace=ws, key=key).first()
    if row is None:
        seen = rolecatalog.find(ws.slug, key)
        if seen is not None:
            raise HttpError(404, f"{key!r} is defined on {seen.workspace_id}; change it there")
        raise HttpError(404, f"no role called {key!r} on {ws.slug}")
    return row


def _out(row: AgentRole, request: HttpRequest, use: dict | None = None, *,
         editable: set[str] | None = None) -> AgentRoleOut:
    base = api_access_list.role_out(row)
    use = use if use is not None else roleadmin.usage(row)
    if editable is None:
        editable = _editable(request)
    return AgentRoleOut(
        key=row.key, label=row.label, description=row.description, permissions=base.permissions,
        can=base.can, defined_on=row.workspace_id, builtin=row.builtin, contains_write=base.contains_write,
        in_use=AgentRoleUseOut(**use),
        can_edit=row.workspace_id in editable)


def _editable(request: HttpRequest) -> set[str]:
    """The workspaces, of those this request may act in, whose roles the caller may define."""
    return perms.slugs_with(request.user, perms.MEMBERS_MANAGE, within=set(wsvc.request_member_slugs(request)))


def _need_rights(request: HttpRequest, ws: Workspace, *, before=None, after=None) -> bool:
    """The workspace admin may define roles; a role that drives or changes the agent, before or after the
    change, is the owner's. Returns whether it drives (the step-up rule asks)."""
    if not perms.can(request.user, ws, perms.MEMBERS_MANAGE):
        raise HttpError(403, "defining a workspace's roles takes a workspace admin")
    driving = any(roles.drives(held) for held in (before, after) if held is not None)
    if driving and not perms.can(request.user, ws, perms.OWN):
        raise HttpError(403, "a role that lets people drive or change the agent is the workspace owner's "
                             "to define or change")
    return driving


def _machine_may_not(request: HttpRequest, driving: bool) -> None:
    """A token cannot touch a passkey: it may write a role that only reads or opens host sites (the admin
    rule and the audit still apply) and never one that drives or changes the agent. An agent's own login
    is no person at all."""
    stepup.require_a_person(request, "defining a role")
    if driving:
        raise HttpError(403, "a role that drives or changes the agent is defined by a person at a browser, "
                             "confirming with a passkey; a token cannot")


def _closed(row: AgentRole):
    return roleadmin.closed(row)


def _audit(ws: Workspace, key: str, before, after, summary: str, use: dict) -> None:
    audit.note_change(scope=f"agent-roles:{ws.slug}:{key}", before=before, after=after,
                      summary=f"{summary}; {roleadmin.blast(use)}")


# --- reading ---------------------------------------------------------------------------

@router.get("/{slug}/agent-roles", response=AgentRolesOut,
            summary="The roles an access-list line in this workspace may give: its own and those above, with what each can do")
@gate("member")
def list_agent_roles(request: HttpRequest, slug: str) -> AgentRolesOut:
    ws = _workspace_or_404(request, slug)
    visible = rolecatalog.visible(ws.slug)
    counts = {r["pk"]: r for r in AgentRole.objects.filter(pk__in=[r.pk for r in visible]).annotate(
        n_lines=Count("lines"), n_agents=Count("lines__agent", distinct=True),
        n_defaults=Count("lines", filter=Q(lines__agent__isnull=True))).values(
        "pk", "n_lines", "n_agents", "n_defaults")}
    out = []
    editable = _editable(request)
    for row in visible:
        c = counts[row.pk]
        out.append(_out(row, request, {"lines": c["n_lines"], "agents": c["n_agents"], "defaults": c["n_defaults"]},
                        editable=editable))
    return AgentRolesOut(
        roles=out,
        fixed=[FixedRoleOut(key=r, label=r.title(), can=api_access_list.role_can(r), why=_FIXED_WHY[r])
               for r in (roles.OWNER, roles.ADMIN)],
        can_edit=perms.can(request.user, ws, perms.MEMBERS_MANAGE),
        can_give_write=perms.can(request.user, ws, perms.OWN))


# --- writing ---------------------------------------------------------------------------

@router.post("/{slug}/agent-roles", response={201: AgentRoleOut},
             summary="Define a role: a name and what it allows (a workspace admin; the owner for one that drives or changes the agent)")
@gate("members.manage", setting=True, step_up=True)
def create_agent_role(request: HttpRequest, slug: str, payload: AgentRoleIn) -> Status:
    ws = _workspace_or_404(request, slug)
    try:
        wanted = roles.validate_permissions(payload.permissions)
    except roles.InvalidRole as exc:
        raise HttpError(422, str(exc)) from exc
    driving = _need_rights(request, ws, after=wanted)
    if stepup.is_browser(request):
        stepup.require_recent_step_up(request)
    else:
        _machine_may_not(request, driving)
    try:
        row = roleadmin.create(ws.slug, key=payload.key, label=payload.label, description=payload.description,
                               permissions=payload.permissions, actor=request.user)
    except roleadmin.RoleError as exc:
        raise HttpError(exc.status, str(exc)) from exc
    _audit(ws, row.key, None, roleadmin.snapshot(row), f"defined the role {row.key!r} on {ws.slug}",
           {"lines": 0, "agents": 0, "defaults": 0})
    return Status(201, _out(row, request))


@router.patch("/{slug}/agent-roles/{key}", response=AgentRoleOut,
              summary="Change a role's name, description or what it allows; its holders change from the next request")
@gate("members.manage", setting=True, step_up=True)
def change_agent_role(request: HttpRequest, slug: str, key: str, payload: AgentRolePatch) -> AgentRoleOut:
    ws = _workspace_or_404(request, slug)
    row = _role_or_404(ws, key)
    before_set = _closed(row)
    try:
        after_set = roles.validate_permissions(payload.permissions) if payload.permissions is not None else before_set
    except roles.InvalidRole as exc:
        raise HttpError(422, str(exc)) from exc
    driving = _need_rights(request, ws, before=before_set, after=after_set)
    if stepup.is_browser(request):
        stepup.require_recent_step_up(request)
    else:
        _machine_may_not(request, driving)
    before = roleadmin.snapshot(row)
    try:
        row = roleadmin.update(row, label=payload.label, description=payload.description,
                               permissions=payload.permissions)
    except roleadmin.RoleError as exc:
        raise HttpError(exc.status, str(exc)) from exc
    _audit(ws, row.key, before, roleadmin.snapshot(row), f"changed the role {row.key!r} on {ws.slug}",
           roleadmin.usage(row))
    return _out(row, request)


@router.post("/{slug}/agent-roles/{key}/reset", response=AgentRoleOut,
             summary="Put a built-in role back to its defaults")
@gate("members.manage", setting=True, step_up=True)
def reset_agent_role(request: HttpRequest, slug: str, key: str) -> AgentRoleOut:
    ws = _workspace_or_404(request, slug)
    row = _role_or_404(ws, key)
    seed = next((s for s in roles.SEED if s.key == row.key), None)
    before_set = _closed(row)
    after_set = roles.ROLES.close(seed.permissions) if seed else before_set
    driving = _need_rights(request, ws, before=before_set, after=after_set)
    if stepup.is_browser(request):
        stepup.require_recent_step_up(request)
    else:
        _machine_may_not(request, driving)
    before = roleadmin.snapshot(row)
    try:
        row = roleadmin.reset(row)
    except roleadmin.RoleError as exc:
        raise HttpError(exc.status, str(exc)) from exc
    _audit(ws, row.key, before, roleadmin.snapshot(row), f"reset the role {row.key!r} on {ws.slug} to its defaults",
           roleadmin.usage(row))
    return _out(row, request)


@router.delete("/{slug}/agent-roles/{key}", response={204: None},
               summary="Delete a role nobody holds, or move its holders to another role in the same step (reassign_to)")
@gate("members.manage", setting=True, step_up=True)
def delete_agent_role(request: HttpRequest, slug: str, key: str, reassign_to: str = ""):
    ws = _workspace_or_404(request, slug)
    row = _role_or_404(ws, key)
    driving = _need_rights(request, ws, before=_closed(row))
    target = None
    if reassign_to:
        target = rolecatalog.find(ws.slug, reassign_to)
        if target is None:
            raise HttpError(404, f"no role called {reassign_to!r} to move its holders to")
        if roles.drives(_closed(target)):
            if not perms.can(request.user, ws, perms.OWN):
                raise HttpError(403, "moving people to a role that drives or changes the agent is the workspace "
                                     "owner's to do")
            # Moving holders onto a driving role hands them what it gives, so the passkey rule covers it.
            driving = True
    if stepup.is_browser(request):
        stepup.require_recent_step_up(request)
    else:
        _machine_may_not(request, driving)
    before, use = roleadmin.snapshot(row), roleadmin.usage(row)
    try:
        moved = roleadmin.delete(row, reassign_to=target)
    except roleadmin.RoleError as exc:
        raise HttpError(exc.status, str(exc)) from exc
    note = f"deleted the role {key!r} on {ws.slug}" + (f", moving {moved} line(s) to {reassign_to!r}" if moved else "")
    _audit(ws, key, before, None, note, use)
    return Status(204, None)

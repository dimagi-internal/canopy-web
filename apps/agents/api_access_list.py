"""The access list over HTTP: `/api/agents/{slug}/access-list` and a workspace's defaults.

Any member may
READ an agent's list (like its roster); an agent admin changes it. A workspace's default lines
— the ones every agent in it and below inherits — are changed by a workspace admin, except that
a default that gives the whole agent (`editor`) is the workspace owner's call.
"""
from __future__ import annotations

from django.db import IntegrityError, transaction
from django.http import HttpRequest
from ninja import Router, Status
from ninja.errors import HttpError

from apps.api.errors import refusals
from apps.api.route_gates import gate
from apps.events import audit
from apps.workspaces import permissions as perms
from apps.workspaces import services as wsvc
from apps.workspaces.models import Workspace

from . import access, profile_settings, grants, rolecatalog, roles
from .gates import agent_for_admin, agent_for_read
from .published_profiles import is_read_mode
from .models import Agent, AgentGrant
from .schemas import (
    AccessImplicitOut,
    AccessInheritIn,
    AccessLineIn,
    AccessLineOut,
    AccessLinePatch,
    AccessListOut,
    AccessRoleOut,
    AgentDefaultsOut,
    HostSiteChoiceOut,
)

router = Router(tags=["agents"])
defaults_router = Router(tags=["workspaces"])

#: The words a person sees beside each permission.
_CAN = {roles.READ: "ask it questions in read mode (it can only read and draft a reply)",
        roles.WRITE: "drive the whole agent", roles.AGENT_EDIT: "change its skills and settings",
        roles.ADMINISTER: "configure it", roles.OWN: "transfer or delete it"}
_PERMISSIONS = (roles.READ, roles.WRITE, roles.AGENT_EDIT, roles.ADMINISTER, roles.OWN)


def _out(line: AgentGrant, *, can_edit: bool, warning: str = "") -> AccessLineOut:
    return AccessLineOut(
        warning=warning, id=line.pk, scope="agent" if line.agent_id else "workspace",
        scope_workspace=line.workspace_id, kind=line.kind, who=grants.describe(line), role=line.role,
        host_site=line.capability, include_below=line.include_below,
        user_id=line.user_id, subject_workspace=line.subject_workspace_id, at_least=line.at_least,
        domain=line.domain, verified=line.verified, manual_only=line.manual_only, note=line.note,
        granted_by_email=getattr(line.granted_by, "email", None), granted_at=line.granted_at,
        can_edit=can_edit)


def _can(permissions) -> list[str]:
    """What a role with these (closed) permissions lets someone do, in words."""
    sites = sorted(p.partition(":")[2] for p in permissions if p.startswith(f"{roles.USE}:") and p != f"{roles.USE}:*")
    out = [_CAN[p] for p in _PERMISSIONS if roles.holds(permissions, p)]
    return out + [f"use it from the {d!r} host site" for d in sites if not roles.holds(permissions, roles.WRITE)]


def role_can(role: str) -> list[str]:
    """What a fixed role (`owner`, `admin`) lets someone do, in words."""
    return _can(roles.permissions_of(role))


def role_out(row) -> AccessRoleOut:
    """A catalog role as the access-list forms show it."""
    held = roles.ROLES.close(row.permissions)
    plain = [p for p in _PERMISSIONS if p in held]
    sites = sorted(p for p in held if p.startswith(f"{roles.USE}:") and p != f"{roles.USE}:*")
    return AccessRoleOut(role=row.key, can=_can(held), permissions=plain + sites, label=row.label,
                         description=row.description, contains_write=roles.drives(held),
                         defined_on=row.workspace_id)


def _roles_out(home: str) -> list[AccessRoleOut]:
    """The fixed roles (`owner`, `admin`: they come with the agent, never from a line), then every role a
    line under `home` may give, strongest first."""
    fixed = [AccessRoleOut(role=r, can=_can(roles.permissions_of(r)), label=r.title(), fixed=True,
                           permissions=[p for p in _PERMISSIONS if roles.allows(r, p)],
                           contains_write=True)
             for r in (roles.OWNER, roles.ADMIN)]
    return fixed + [role_out(row) for row in rolecatalog.visible(home)]


def _implicit(agent: Agent) -> list[AccessImplicitOut]:
    out = []
    for row in access.roster(agent):
        if row["agent_role"] in (access.OWNER, access.ADMIN):
            out.append(AccessImplicitOut(who=row["email"], role=row["agent_role"], why=row["basis"]))
    return out


def _fields(payload: AccessLineIn, actor, home: str) -> dict:
    try:
        fields = grants.check_line(
            actor, home=home, kind=payload.kind, role=payload.role, capability=payload.host_site,
            user_id=payload.user_id, email=payload.email, workspace=payload.workspace,
            at_least=payload.at_least, domain=payload.domain, verified=payload.verified,
            include_below=payload.include_below)
    except grants.LineError as exc:
        raise HttpError(422, str(exc)) from exc
    # An approval gate is about driving the whole agent; a host-site line has nothing to gate.
    fields.update(manual_only=payload.manual_only and bool(fields["role"]), note=payload.note)
    return fields


def _create(scope: dict, fields: dict, actor) -> AgentGrant:
    line = AgentGrant(**scope, granted_by=actor, **fields)
    line.subject_key = AgentGrant.key_of(
        line.kind, user_id=line.user_id, subject_workspace=line.subject_workspace_id,
        at_least=line.at_least, domain=line.domain, include_below=line.include_below,
        site=line.capability)
    if AgentGrant.objects.filter(**scope, subject_key=line.subject_key).exists():
        raise HttpError(409, f"{grants.describe(line)} is already on the list — change that line instead")
    try:
        with transaction.atomic():
            grants.lock_role_for(line)
            line.save()
    except grants.LineError as exc:
        raise HttpError(422, str(exc)) from exc
    except IntegrityError as exc:  # a concurrent identical add loses the unique constraint
        raise HttpError(409, "that line is already on the list") from exc
    return line


def _summary(line: AgentGrant) -> dict:
    return {"who": grants.describe(line), "role": line.role, "capability": line.capability,
            "manual_only": line.manual_only}


def _host_site_warning(line: AgentGrant, agent: Agent) -> str:
    """Advice, never a refusal: a line names a host site this agent does not have, so it opens
    nothing until it does."""
    if not line.capability or is_read_mode(line.capability) \
            or line.capability in ((agent.interface or {}).get("capabilities") or {}):
        return ""   # (read mode exists on every agent)
    return (f"{agent.slug} has no host site called {line.capability!r}, so this line "
            "opens nothing until it does")


# --- an agent's list --------------------------------------------------------------------

@router.get("/{slug}/access-list", response=AccessListOut,
            summary="Who has which role on this agent: its own lines, the workspace defaults it inherits, and the roles that come with it")
@gate("member")
def get_access_list(request: HttpRequest, slug: str) -> AccessListOut:
    """The agent's access list (`(who, role)` lines), the workspace-wide lines it inherits
    (`scope: workspace`, changed on the workspace), and the owner and admins who hold their
    role without a line. A line's role is the strongest of the lines that name a person."""
    agent = agent_for_read(request, slug)
    admin = agent.is_admin(request.user)
    lines = [_out(ln, can_edit=admin and ln.agent_id is not None) for ln in grants.lines_for(agent)]
    return AccessListOut(
        inherit_workspace_defaults=agent.inherit_workspace_defaults,
        lines=lines, implicit=_implicit(agent), roles=_roles_out(agent.workspace_id), can_edit=admin,
        host_sites=_host_sites(agent))


def _host_sites(agent: Agent) -> list[HostSiteChoiceOut]:
    """Every entry a line may name (all but read mode), with the site it is connected to."""
    site_of = {row["key"]: row["site"] for row in profile_settings.host_sites_of(agent)}
    names = sorted(n for n in ((agent.interface or {}).get("capabilities") or {}) if not is_read_mode(n))
    return [HostSiteChoiceOut(name=n, site=site_of.get(n, "")) for n in names]


@router.post("/{slug}/access-list", response={201: AccessLineOut, **refusals(422)},
             summary="Add a line to this agent's access list (an agent admin)")
@gate("agent-admin", setting=True)
def add_access_line(request: HttpRequest, slug: str, payload: AccessLineIn) -> Status:
    """A line says who has what: a person, everyone in a workspace (optionally also any workspace
    below it), anyone at an email domain, or a known visitor or sender. What they get is a role
    (a key from `GET /api/workspaces/{ws}/agent-roles`: the built-ins read_only, read_write and editor,
    and any a workspace defined) or, rarely, one host site (`host_site`, a name from the list's
    `host_sites`). A line that names people only by address (domain, contact) can give a role that
    only reads or opens host sites, never one that drives or changes the agent.
    A line gives a role or one host site, not both and not neither (422)."""
    agent = agent_for_admin(request, slug)
    line = _create({"agent": agent}, _fields(payload, request.user, agent.workspace_id), request.user)
    audit.note_change(scope=f"access-list:{agent.slug}", before=None, after=_summary(line),
                      summary=f"added {grants.describe(line)} as {line.role or 'the host site ' + line.capability} "
                              f"on {agent.slug}")
    warning = grants.domain_warning(line, agent.workspace_id) or _host_site_warning(line, agent)
    return Status(201, _out(line, can_edit=True, warning=warning))


def _own_line(agent: Agent, line_id: int) -> AgentGrant:
    line = AgentGrant.objects.filter(pk=line_id, agent=agent).select_related("user").first()
    if line is None:
        raise HttpError(404, "no such line on this agent's own list (inherited lines are changed on the workspace)")
    return line


@router.patch("/{slug}/access-list/{line_id}", response=AccessLineOut,
              summary="Change a line's role, its manual-only flag or its note (an agent admin)")
@gate("agent-admin", setting=True)
def change_access_line(request: HttpRequest, slug: str, line_id: int, payload: AccessLinePatch) -> AccessLineOut:
    agent = agent_for_admin(request, slug)
    line = _own_line(agent, line_id)
    before = _summary(line)
    _apply_patch(line, payload, agent.workspace_id)
    audit.note_change(scope=f"access-list:{agent.slug}", before=before, after=_summary(line),
                      summary=f"changed {grants.describe(line)} on {agent.slug}")
    return _out(line, can_edit=True)


def _apply_patch(line: AgentGrant, payload: AccessLinePatch, home: str) -> None:
    if payload.role is not None:
        if line.capability:
            raise HttpError(422, "this line opens one host site; remove it and add a role line")
        try:
            row = grants.check_role(line.kind, payload.role, home)
        except grants.LineError as exc:
            raise HttpError(422, str(exc)) from exc
        line.role, line.role_def = payload.role, row
    if payload.manual_only is not None:
        line.manual_only = payload.manual_only
    if payload.note is not None:
        line.note = payload.note
    try:
        with transaction.atomic():
            grants.lock_role_for(line)
            line.save(update_fields=["role", "role_def", "manual_only", "note"])
    except grants.LineError as exc:
        raise HttpError(422, str(exc)) from exc


@router.delete("/{slug}/access-list/{line_id}", response={204: None},
               summary="Take a line off this agent's access list (an agent admin)")
@gate("agent-admin", setting=True)
def remove_access_line(request: HttpRequest, slug: str, line_id: int):
    agent = agent_for_admin(request, slug)
    line = _own_line(agent, line_id)
    audit.note_change(scope=f"access-list:{agent.slug}", before=_summary(line), after=None,
                      summary=f"removed {grants.describe(line)} from {agent.slug}")
    line.delete()
    return Status(204, None)


@router.put("/{slug}/inherit-workspace-defaults", response=AccessListOut,
            summary="Say whether this agent also takes its workspace's default lines (an agent admin)")
@gate("agent-admin", setting=True)
def set_inherit_workspace_defaults(request: HttpRequest, slug: str, payload: AccessInheritIn) -> AccessListOut:
    """On by default: a workspace-wide line reaches every agent in it and below. An agent
    holding credentials people should be named for turns it off and lists its own."""
    agent = agent_for_admin(request, slug)
    before = agent.inherit_workspace_defaults
    agent.inherit_workspace_defaults = payload.inherit
    agent.save(update_fields=["inherit_workspace_defaults", "updated_at"])
    audit.note_change(scope=f"access-list:{agent.slug}", before={"inherit_workspace_defaults": before},
                      after={"inherit_workspace_defaults": payload.inherit},
                      summary=f"{agent.slug} {'takes' if payload.inherit else 'does not take'} its workspace's default lines")
    return get_access_list(request, slug)


# --- a workspace's default lines ----------------------------------------------------------

def _workspace_or_404(request: HttpRequest, slug: str) -> Workspace:
    ws = Workspace.objects.filter(slug=slug).first()
    if ws is None or slug not in wsvc.request_member_slugs(request):
        raise HttpError(404, f"workspace {slug!r} not found")
    return ws


@defaults_router.get("/{slug}/agent-defaults", response=AgentDefaultsOut,
                     summary="The default access-list lines every agent in this workspace and below inherits")
@gate("member")
def list_agent_defaults(request: HttpRequest, slug: str) -> AgentDefaultsOut:
    ws = _workspace_or_404(request, slug)
    manage = perms.can(request.user, ws, perms.MEMBERS_MANAGE)
    return AgentDefaultsOut(
        lines=[_out(ln, can_edit=manage) for ln in
               AgentGrant.objects.filter(workspace=ws).select_related("user", "granted_by")],
        roles=_roles_out(ws.slug), can_edit=manage, can_give_write=perms.can(request.user, ws, perms.OWN))


def _require_default_rights(request: HttpRequest, ws: Workspace, role: str) -> None:
    row = rolecatalog.role_for(ws.slug, role) if role else None
    if not perms.can(request.user, ws, perms.MEMBERS_MANAGE):
        raise HttpError(403, "changing a workspace's default access takes a workspace admin")
    # By permission, not by name: any role that lets people drive (or change) every agent here.
    if grants.grants_write(row) and not perms.can(request.user, ws, perms.OWN):
        raise HttpError(403, "a default that lets people drive every agent in the workspace (or change them) "
                             f"({role}) is the workspace owner's to set")


@defaults_router.post("/{slug}/agent-defaults", response={201: AccessLineOut},
                      summary="Add a default line: every agent here and below inherits it unless it opts out")
@gate("members.manage", setting=True)
def add_agent_default(request: HttpRequest, slug: str, payload: AccessLineIn) -> Status:
    ws = _workspace_or_404(request, slug)
    _require_default_rights(request, ws, payload.role)
    line = _create({"workspace": ws}, _fields(payload, request.user, ws.slug), request.user)
    audit.note_change(scope=f"agent-defaults:{ws.slug}", before=None, after=_summary(line),
                      summary=f"added {grants.describe(line)} as {line.role} on every agent in {ws.slug}")
    return Status(201, _out(line, can_edit=True, warning=grants.domain_warning(line, ws.slug)))


@defaults_router.delete("/{slug}/agent-defaults/{line_id}", response={204: None},
                        summary="Take a default line away")
@gate("members.manage", setting=True)
def remove_agent_default(request: HttpRequest, slug: str, line_id: int):
    ws = _workspace_or_404(request, slug)
    line = AgentGrant.objects.filter(pk=line_id, workspace=ws).select_related("user").first()
    if line is None:
        raise HttpError(404, "no such default line")
    _require_default_rights(request, ws, line.role)
    audit.note_change(scope=f"agent-defaults:{ws.slug}", before=_summary(line), after=None,
                      summary=f"removed {grants.describe(line)} from every agent in {ws.slug}")
    line.delete()
    return Status(204, None)

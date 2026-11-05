"""Defining, editing, resetting and deleting the roles of a workspace's catalog.

The rules (`docs/superpowers/specs/2026-11-04-configurable-roles-design.md`, sections 3, 4 and 10),
all by permission and never by a role's name:

* A key is a slug, unique along a workspace chain (an ancestor's role is visible below and cannot be
  redefined), never one the platform reserves.
* The permissions are the fixed vocabulary at the read / write / agent_edit level plus `use:<site>`,
  stored closed (`roles.validate_permissions`). Never `admin` or `own`.
* An edit that makes a role drive or change the agent (`write`, which `agent_edit` carries) is refused
  while an address-only line (`domain`, `contact`) holds it: that line would silently begin to drive every
  agent it reaches.
* A role in use is not deleted; its lines are reassigned in the same step, or the delete is refused with
  the lines named. The database says the same (`AgentGrant.role_def` is PROTECT).

Raises `RoleError` with a status and a message fit to show beside the form. Callers (the API) decide who
may do it; `drives_before_or_after` is what makes that the workspace owner.
"""
from __future__ import annotations

import re

from django.core.exceptions import ValidationError
from django.db import transaction
from django.db.models import Q

from apps.workspaces import services as wsvc

from . import grants, rolecatalog, roles
from .models import AgentGrant, AgentRole

_KEY = re.compile(r"^[a-z][a-z0-9_]{0,39}$")
MAX_NAMED = 20      # lines named in a refusal, so a message stays readable


class RoleError(ValueError):
    def __init__(self, message: str, status: int = 422):
        super().__init__(message)
        self.status = status


def closed(row: AgentRole) -> frozenset[str]:
    return roles.ROLES.close(row.permissions)


def drives(row: AgentRole) -> bool:
    return roles.drives(closed(row))


def snapshot(row: AgentRole) -> dict:
    return {"key": row.key, "label": row.label, "description": row.description,
            "permissions": sorted(closed(row)), "workspace": row.workspace_id}


def home_of(line: AgentGrant) -> str:
    return line.workspace_id or line.agent.workspace_id


def describe_line(line: AgentGrant) -> str:
    where = (f"{line.agent.slug}" if line.agent_id else f"the defaults of {line.workspace_id}")
    return f"{grants.describe(line)} on {where}"


def lines_of(row: AgentRole):
    return AgentGrant.objects.filter(role_def=row).select_related("agent", "user", "workspace")


def usage(row: AgentRole) -> dict:
    """{lines, agents, defaults}: how many lines hold the role, on how many agents and workspace defaults."""
    qs = AgentGrant.objects.filter(role_def=row)
    return {"lines": qs.count(), "agents": qs.exclude(agent__isnull=True).values("agent").distinct().count(),
            "defaults": qs.filter(agent__isnull=True).count()}


def blast(use: dict) -> str:
    return (f"{use['lines']} access-list line(s) hold it ({use['agents']} agent(s), "
            f"{use['defaults']} workspace default(s))")


def _named(lines) -> str:
    names = [describe_line(ln) for ln in lines[:MAX_NAMED]]
    more = len(lines) - len(names)
    return "; ".join(names) + (f"; and {more} more" if more > 0 else "")


# --- the checks ---------------------------------------------------------------------------

def _check_key(workspace_slug: str, key: str) -> str:
    key = (key or "").strip()
    if not _KEY.match(key):
        raise RoleError("a role's key is lowercase letters, digits and underscores, starting with a letter "
                        "(40 at most)")
    if key in roles.RESERVED_KEYS:
        raise RoleError(f"{key!r} is reserved: it names something fixed in canopy, never a role you can give")
    chain = rolecatalog.chain_of(workspace_slug)
    held = AgentRole.objects.filter(key=key, workspace_id__in=chain).first()
    if held is not None:
        where = "this workspace" if held.workspace_id == workspace_slug else f"{held.workspace_id} (above)"
        raise RoleError(f"a role called {key!r} already exists in {where}; a role means one thing everywhere "
                        "it is visible, so it cannot be defined again below", status=409)
    below = wsvc.descendant_slugs({workspace_slug})
    clash = AgentRole.objects.filter(key=key, workspace_id__in=below).first() if below else None
    if clash is not None:
        raise RoleError(f"{clash.workspace_id}, below this workspace, already defines a role called {key!r}; "
                        "remove or rename it there first", status=409)
    return key


def _check_text(label: str, description: str) -> tuple[str, str]:
    label = (label or "").strip()
    if not label or len(label) > 80:
        raise RoleError("a role needs a name of 80 characters or fewer")
    description = (description or "").strip()
    if len(description) > 300:
        raise RoleError("the description is 300 characters at most")
    return label, description


def _address_only_holders(row: AgentRole) -> list[AgentGrant]:
    return list(lines_of(row).filter(kind__in=grants.ADDRESS_ONLY))


def _refuse_if_address_lines_would_drive(row: AgentRole, new: frozenset[str]) -> None:
    gained = ({roles.WRITE, roles.AGENT_EDIT} & set(new)) - set(closed(row))
    if gained:
        holders = _address_only_holders(row)
        if holders:
            raise RoleError(
                f"{len(holders)} line(s) that name people only by their address hold {row.key!r} "
                f"({_named(holders)}); making it drive or change the agent would let anyone who can claim "
                "that address do so. Remove those lines, or copy the role instead")


# --- the writes ---------------------------------------------------------------------------

def create(workspace_slug: str, *, key, label, description, permissions, actor) -> AgentRole:
    key = _check_key(workspace_slug, key)
    label, description = _check_text(label, description)
    try:
        held = roles.validate_permissions(permissions)
    except roles.InvalidRole as exc:
        raise RoleError(str(exc)) from exc
    top = AgentRole.objects.filter(workspace_id=workspace_slug).order_by("-position").first()
    return AgentRole.objects.create(
        workspace_id=workspace_slug, key=key, label=label, description=description,
        permissions=sorted(held), builtin=False, position=(top.position + 1) if top else 0, created_by=actor)


@transaction.atomic
def update(row: AgentRole, *, label=None, description=None, permissions=None) -> AgentRole:
    if label is not None or description is not None:
        row.label, row.description = _check_text(row.label if label is None else label,
                                                 row.description if description is None else description)
    if permissions is not None:
        try:
            held = roles.validate_permissions(permissions)
        except roles.InvalidRole as exc:
            raise RoleError(str(exc)) from exc
        AgentRole.objects.select_for_update().filter(pk=row.pk).first()  # against `grants.lock_role_for`
        _refuse_if_address_lines_would_drive(row, held)
        row.permissions = sorted(held)
    row.save()
    return row


@transaction.atomic
def reset(row: AgentRole) -> AgentRole:
    seed = next((s for s in roles.SEED if s.key == row.key), None)
    if not row.builtin or seed is None:
        raise RoleError("only a built-in role has defaults to go back to", status=409)
    return update(row, label=seed.label, description=seed.description, permissions=seed.permissions)


@transaction.atomic
def delete(row: AgentRole, *, reassign_to: AgentRole | None = None) -> int:
    """Remove the role; returns how many lines were reassigned."""
    if row.builtin:
        raise RoleError("a built-in role cannot be deleted (rename or reshape it instead)", status=409)
    lines = list(lines_of(row))
    if lines and reassign_to is None:
        raise RoleError(f"{len(lines)} line(s) still hold {row.key!r}: {_named(lines)}. Remove them, or "
                        "delete with reassign_to=<another role>", status=409)
    if lines:
        if reassign_to.pk == row.pk:
            raise RoleError("reassign_to is the role being deleted")
        for ln in lines:
            home = home_of(ln)
            if rolecatalog.find(home, reassign_to.key) != reassign_to:
                raise RoleError(f"{reassign_to.key!r} is not available to {describe_line(ln)}", status=409)
            if ln.kind in grants.ADDRESS_ONLY and drives(reassign_to):
                raise RoleError(f"{describe_line(ln)} names people only by their address and cannot take "
                                f"{reassign_to.key!r}, which drives or changes the agent", status=409)
        for ln in lines:
            ln.role, ln.role_def = reassign_to.key, reassign_to
            ln.save(update_fields=["role", "role_def"])
    row.delete()
    return len(lines)


# --- a workspace moving in the tree --------------------------------------------------------

def carry_lines_after_move(ws) -> None:
    """A line holds its role's ROW, so once `ws` has moved, a line in it or below may hold a row its new
    chain does not show: one another tree's owners still edit, or one hidden by a root it was nested
    under. Each such line is re-pointed to the role of that key it can see when that role allows exactly
    the same; otherwise the move is refused, naming the lines. Runs inside the save, so a refusal undoes it."""
    slugs = {ws.pk, *wsvc.descendant_slugs({ws.pk})}
    lines = (AgentGrant.objects.filter(Q(workspace_id__in=slugs) | Q(agent__workspace_id__in=slugs))
             .exclude(role_def__isnull=True).select_related("agent", "role_def", "user"))
    stranded = []
    for line in lines:
        there = rolecatalog.role_for(home_of(line), line.role)
        if there is not None and there.pk == line.role_def_id:
            continue
        if there is None or closed(there) != closed(line.role_def):
            stranded.append(line)
            continue
        line.role_def = there
        line.save(update_fields=["role_def"])
    if stranded:
        raise ValidationError(f"{len(stranded)} access-list line(s) hold the role(s) "
                              f"{', '.join(sorted({ln.role for ln in stranded}))}, which mean something else or do "
                              f"not exist where {ws.pk} would be ({_named(stranded)}); give them a role that "
                              "exists there first")

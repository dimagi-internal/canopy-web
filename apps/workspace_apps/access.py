"""Who holds which of an app's roles — built on the workspace permission table.

An app never invents its own notion of trust. Each of its roles is a WORKSPACE
capability (`apps/workspaces/permissions.py`) asked through `perms.can`, plus
any explicit `AppRoleGrant`, and only a workspace member can hold one. So
nesting, inheritance and every later change to what a workspace role means
reach every app with no edit here, and `tests/test_roles_named_only_in_workspaces.py`
still holds: nothing in this module names a workspace role.

Three tiers act on the app itself, beside its own roles:

* MANAGE (`integrations`, an admin): install, switch on/off, make public, remove,
  and publish a change that alters what the public sees or who can do what.
* MAINTAIN (managers, or anyone granted the app's `maintainer` role — the head
  of HR for Hiring): open a draft, change it, test it, publish ordinary changes,
  roll back. Not a workspace role: running the hiring app is not running the
  workspace.
* GRANT (`members.manage`): give or take app roles by name.
"""
from __future__ import annotations

from apps.workspaces import permissions as perms

from .models import AppInstallation, AppRoleGrant

#: The grant-only role that maintains an app, present in every app.
MAINTAINER = "maintainer"


def roles_for(user, installation: AppInstallation, manifest: dict | None = None) -> set[str]:
    """Every role `user` holds in `installation`. Always contains `public`;
    `member` and every other role need membership of the app's workspace."""
    held = {"public"}
    ws = installation.workspace_id
    if not perms.can(user, ws, perms.READ):
        return held
    held.add("member")
    for name, spec in ((manifest or installation.manifest).get("roles") or {}).items():
        cap = spec.get("capability")
        if cap and perms.can(user, ws, cap):
            held.add(name)
    held |= set(AppRoleGrant.objects.filter(installation=installation, user=user).values_list("role", flat=True))
    return held


def may_manage(user, workspace) -> bool:
    """Install, switch on/off, make public, remove, approve a risky change."""
    return perms.can(user, workspace, perms.INTEGRATIONS)


def may_maintain(user, installation: AppInstallation) -> bool:
    """Draft, test, publish ordinary changes and roll back this app."""
    if may_manage(user, installation.workspace_id):
        return True
    return (perms.can(user, installation.workspace_id, perms.READ)
            and AppRoleGrant.objects.filter(installation=installation, user=user, role=MAINTAINER).exists())


def may_grant(user, workspace) -> bool:
    """Give or take an app role by name: the tier that already manages members."""
    return perms.can(user, workspace, perms.MEMBERS_MANAGE)

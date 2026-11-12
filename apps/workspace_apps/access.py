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

A GUEST of the workspace (`WorkspaceMembership.GUEST`, docs/architecture/access.md § Guests)
is no member: `perms.can` answers no for them on every permission. `roles_for` opts in for them
alone, and gives a guest exactly the app role their scope names for THIS app, beside `public`
— never `member`, never a capability role, never a grant, and nothing in any other app.
`validate_guest_apps` is what a guest scope may name, registered with `workspaces.guests`.
"""
from __future__ import annotations

from apps.workspaces import guests
from apps.workspaces import permissions as perms
from apps.workspaces import services as wsvc

from .models import AppInstallation, AppRoleGrant

#: The grant-only role that maintains an app, present in every app.
MAINTAINER = "maintainer"


def roles_for(user, installation: AppInstallation, manifest: dict | None = None) -> set[str]:
    """Every role `user` holds in `installation`. Always contains `public`;
    `member` and every other role need membership of the app's workspace."""
    held = {"public"}
    ws = installation.workspace_id
    if not perms.can(user, ws, perms.READ):
        guest_role = guest_role_for(user, installation, manifest)
        return held | ({guest_role} if guest_role else set())
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


def is_helper(user, draft) -> bool:
    """Is `user` an agent a maintainer asked to work on this open draft? Still
    only while it is a member of the workspace and the draft is open."""
    return (getattr(user, "pk", None) is not None and draft.status == draft.DRAFT
            and perms.can(user, draft.installation.workspace_id, perms.READ)
            and draft.helpers.filter(pk=user.pk).exists())


def may_help(agent_user, workspace) -> bool:
    """May this agent login be asked to work on an app's draft here? Only an
    agent's own login (never a person's), and only a member of the workspace."""
    return (agent_user is not None and hasattr(agent_user, "agent_identity")
            and perms.can(agent_user, workspace, perms.READ))


def may_grant(user, workspace) -> bool:
    """Give or take an app role by name: the tier that already manages members."""
    return perms.can(user, workspace, perms.MEMBERS_MANAGE)


def guest_role_for(user, installation: AppInstallation, manifest: dict | None = None) -> str | None:
    """The app role a GUEST of the app's workspace holds in this app — the one their scope
    names, and only while the app still declares it — else None. A member is never a guest."""
    if not getattr(user, "is_authenticated", False):
        return None
    role = wsvc.guest_app_role(user, installation.workspace_id, installation.key)
    if role is None or role not in ((manifest or installation.manifest).get("roles") or {}):
        return None
    return role


def is_guest_of(user, installation: AppInstallation) -> bool:
    """Does `user` reach this app ONLY as a guest of its workspace, with a role in it?"""
    return guest_role_for(user, installation) is not None


def validate_guest_apps(workspace: str, entries: dict, by) -> dict:
    """What a guest scope's `apps` may name (`workspaces.guests.validate_scope`): each an app
    installed here, at a role its manifest declares (never `maintainer`, `member` or
    `public`), and given by someone who may maintain that app."""
    out: dict[str, str] = {}
    for key, role in entries.items():
        inst = AppInstallation.objects.filter(workspace_id=workspace, key=key).first()
        if inst is None:
            raise guests.ScopeError(f"no app {key!r} in {workspace!r}")
        declared = inst.manifest.get("roles") or {}
        if not isinstance(role, str) or role in (MAINTAINER, "member", "public") or role not in declared:
            raise guests.ScopeError(f"app {key!r} has no role {role!r} a guest can hold "
                                    f"(it declares: {', '.join(sorted(declared)) or 'none'})")
        if by is None or not may_maintain(by, inst):
            raise guests.ScopeError(f"giving a guest app {key!r} needs the right to maintain it", status=403)
        out[key] = role
    return out


def describe_guest_apps(workspace: str, entries: dict) -> list[str]:
    """"Household (assistant)" for each app a guest scope names."""
    names = dict(AppInstallation.objects.filter(workspace_id=workspace, key__in=list(entries))
                 .values_list("key", "name"))
    return [f"{names.get(key, key)} ({role})" for key, role in entries.items()]

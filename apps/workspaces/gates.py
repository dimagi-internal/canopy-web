"""Resolving the workspace a route names by slug — the workspace sibling of `agents/gates.py`.

One rule, so every route answers a stranger the same way: a workspace the caller is not
in is a 404 (never a role hint), and one they are in but lack the permission for is a
403 that names the role it needs (`permissions.MINIMUM_ROLE`).
"""
from __future__ import annotations

from ninja.errors import HttpError

from . import permissions as perms
from . import services as wsvc
from .models import Workspace, WorkspaceMembership


def workspace_for(user, slug: str, permission: str | None = None) -> Workspace:
    """The workspace `slug`, if `user` belongs to it and (when given) holds `permission`
    there. 404 for an unknown or foreign workspace; 403 below the permission's role."""
    ws = Workspace.objects.filter(slug=slug).first()
    if ws is None or slug not in wsvc.user_workspace_slugs(user):
        raise HttpError(404, "no such workspace")
    if permission is not None and not perms.can(user, ws, permission):
        raise HttpError(403, f"requires the {perms.MINIMUM_ROLE[permission]} role or above")
    return ws


def membership_or_404(user, slug: str) -> WorkspaceMembership:
    """The caller's membership row, or 404.

    Reads through `services.membership` rather than querying here: a surface needs the
    ROW (to read its workspace and to mutate it), but "am I in this workspace?" must
    still have one implementation — see `tests/test_workspace_authorizer_is_sole_gate.py`.
    """
    m = wsvc.membership(user, slug)
    if m is None:
        raise HttpError(404, f"workspace '{slug}' not found")
    return m


def require(user, slug: str, permission: str) -> WorkspaceMembership:
    """The caller's membership, if their role holds `permission`
    (`permissions.MINIMUM_ROLE`). 404 first — a non-member can't probe roles."""
    m = membership_or_404(user, slug)  # 404 first — a non-member can't probe roles
    if not perms.role_allows(m.role, permission):
        needed = perms.MINIMUM_ROLE[permission]
        raise HttpError(403, f"this requires the {needed} role or above")
    return m


def tree_reader(request, slug: str):
    """The topology/health read pattern: `logs.read` on the root, then each
    descendant filtered by the same permission (owners and admins inherit down)."""
    m = require(request.user, slug, perms.LOGS_READ)
    return m.workspace, (lambda s: perms.can(request.user, s, perms.LOGS_READ))

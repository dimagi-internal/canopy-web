"""Resolving the workspace a route names by slug — the workspace sibling of `agents/gates.py`.

One rule, so every door answers a stranger the same way: a workspace the caller is not
in is a 404 (never a role hint), and one they are in but lack the capability for is a
403 that names the role it needs (`permissions.MINIMUM_ROLE`).
"""
from __future__ import annotations

from ninja.errors import HttpError

from . import permissions as perms
from . import services as wsvc
from .models import Workspace


def workspace_for(user, slug: str, capability: str | None = None) -> Workspace:
    """The workspace `slug`, if `user` belongs to it and (when given) holds `capability`
    there. 404 for an unknown or foreign workspace; 403 below the capability's role."""
    ws = Workspace.objects.filter(slug=slug).first()
    if ws is None or slug not in wsvc.user_workspace_slugs(user):
        raise HttpError(404, "no such workspace")
    if capability is not None and not perms.can(user, ws, capability):
        raise HttpError(403, f"requires the {perms.MINIMUM_ROLE[capability]} role or above")
    return ws

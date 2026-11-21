"""Where one viewer's brain read looks: THE workspace scope the brain page, the graph and the outputs list share.

The read rule every canopy list follows (`workspaces.services.read_scope`): pinned to a workspace, that workspace and
the ones below it the viewer is in (never above it, never a sibling); unpinned, the viewer's workspaces when they sit
in one organisation, else a 422 naming them, so a list never mixes tenants. What each surface shows inside the scope
is still its own rule (`outputs.visible_to`, the session ACL, `project_read_q`); this only says where it looks."""
from __future__ import annotations

from apps.workspaces import services as wsvc


def of_request(request) -> set[str]:
    """The scope of a request: its pin and below, or (unpinned) the viewer's one organisation."""
    return wsvc.request_workspace_slugs(request)


def below(user, workspace: str, *, tree: bool = True) -> set[str]:
    """The scope of a read about `workspace`: it and, with `tree`, the workspaces below it `user` is in."""
    return wsvc.pinned_read_scope(user, workspace) if tree else {workspace}

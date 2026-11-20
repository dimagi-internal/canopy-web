"""`GET /api/workspaces/{slug}/graph`: how work, people, agents and projects relate, as the caller may see them.

A sibling of the workspace network (`/api/workspaces/{slug}/network`, admins, from the analytics fact log): this one
is any member's, and every node and edge in it is one the caller already sees on another surface
(`apps/brain/graph.py`). Reporting lines are drawn only for a person as themselves and never through MCP, as the
reporting route answers; the brain's would-be links (`include=proposed`) only for the person, as the preview
answers (`api_preview._the_person`), and storing nothing."""
from __future__ import annotations

from typing import Literal

from django.http import HttpRequest
from ninja import Router

from apps.api.auth import session_auth
from apps.api.errors import refusals
from apps.api.route_gates import gate
from apps.harness import acting_person
from apps.workspaces.gates import membership_or_404

from . import graph
from .api_admin import manager_leg_open
from .api_preview import _the_person
from .schemas import WorkspaceGraphOut

router = Router(auth=session_auth, tags=["brain"])


@router.get("/workspaces/{slug}/graph", response={200: WorkspaceGraphOut, **refusals(403, 404)},
            summary="How people, agents, sessions, projects and tasks relate, as you may see them")
@gate("member")  # each node and edge by the rule of the surface that already shows it (apps/brain/graph.py)
def workspace_graph(request: HttpRequest, slug: str, days: int = 30, tree: bool = True,
                    include: Literal["", "proposed"] = "") -> dict:
    """People, agents, sessions, projects, tasks, workspaces and learning outputs over the last `days` (1-90), in
    this workspace and, with `tree`, every one below it you are in, each edge typed: who is in a session, who
    started it, which agent ran it, which task dispatched it, which project it is tied to (tier and how), who
    reports to whom, who coaches whom, which sessions an output was learned from. Only what you may already
    see: sessions you read yourself, projects and tasks you may read, reporting lines you are shown, your own
    coaching. `include=proposed` adds the links canopy's learning WOULD make, marked `proposed` and stored
    nowhere (only for you as yourself). Capped; `notes` says what was left out."""
    m = membership_or_404(request.user, slug)
    slugs = graph.tree(request.user, m.workspace) if tree else [m.workspace.slug]
    proposed = include == "proposed"
    if proposed:
        _the_person(request)
    as_person = acting_person.refusal(request) is None and not getattr(request, "via_mcp", False)
    return graph.build(request.user, slugs, days=days, proposed=proposed, as_person=as_person,
                       manager_leg=manager_leg_open(request))

"""The agents of a workspace subtree — one query, in tree order.

Every tree-wide view (runner topology, agent topology, workspace health) walks
`wsvc.subtree` and then needs the agents in it; this is that second half, once.
"""
from __future__ import annotations

from apps.workspaces.models import Workspace

from .models import Agent


def agents_in_tree(tree: list[tuple[Workspace, int]], *related: str) -> list[Agent]:
    """Agents of every workspace in `tree` (`wsvc.subtree`'s output), workspace by
    workspace in tree order, by name within one. `related` is passed to
    select_related."""
    slugs = [ws.slug for ws, _ in tree]
    qs = Agent.objects.filter(workspace_id__in=slugs).order_by("name")
    if related:
        qs = qs.select_related(*related)
    by_ws: dict[str, list[Agent]] = {}
    for a in qs:
        by_ws.setdefault(a.workspace_id, []).append(a)
    return [a for slug in slugs for a in by_ws.get(slug, [])]

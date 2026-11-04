"""The catalog of agent roles a workspace can give: rows (`AgentRole`), seeded and editable.

A role is defined on a workspace and visible to it and every workspace below
(`docs/superpowers/specs/2026-11-04-configurable-roles-design.md`). This module is the only thing
that reads the catalog: the chain a workspace sees, seeding the built-ins, finding a role by key.
What a LINE grants is read from its role's row with the line (`AgentGrant.permissions`); nothing
here is on the decision path and nothing is cached across requests.
"""
from __future__ import annotations

from apps.workspaces.models import Workspace

from . import roles
from .models import AgentRole


def chain_of(workspace_slug: str) -> list[str]:
    """The workspace and every one above it, the root LAST."""
    ws = Workspace.objects.filter(pk=workspace_slug).only("slug", "parent").first()
    return [workspace_slug, *(ws.ancestor_slugs() if ws else [])]


def root_of(workspace_slug: str) -> str:
    return chain_of(workspace_slug)[-1]


def ensure_seeded(workspace_slug: str) -> list[AgentRole]:
    """The three built-ins on `workspace_slug` (a root), created when missing; never edits a
    row that exists: a person's edit to a built-in survives."""
    out = []
    for position, seed in enumerate(roles.SEED):
        row, _ = AgentRole.objects.get_or_create(
            workspace_id=workspace_slug, key=seed.key,
            defaults=dict(label=seed.label, description=seed.description, builtin=True,
                          position=position, permissions=sorted(roles.ROLES.close(seed.permissions))))
        out.append(row)
    return out


def role_for(workspace_slug: str, key: str) -> AgentRole | None:
    """The role `key` as a line on `workspace_slug` sees it: along the chain, the root's first
    (keys are unique along a chain, so there is at most one that matters). A built-in key
    that has no row yet is seeded on the root, so a line can always name a built-in."""
    chain = chain_of(workspace_slug)
    rows = {r.workspace_id: r for r in AgentRole.objects.filter(workspace_id__in=chain, key=key)}
    for slug in reversed(chain):
        if slug in rows:
            return rows[slug]
    if key in roles.SEED_KEYS:
        ensure_seeded(chain[-1])
        return AgentRole.objects.filter(workspace_id=chain[-1], key=key).first()
    return None

"""The connections app's part in moving an agent between workspaces (`apps/agents/move.py`).

Registered from `ConnectionsConfig.ready()`: the move is core and may not import
this app, so the app hands its rows in instead.

An agent's connection belongs to the app (the workspace's OAuth client) that
made it, and is usable only while the agent is in that app's workspace
(`services.agent_connections_usable`). So nothing moves and nothing is written:
the connections stay with their app, and the dry run names each one that stops
working — with every agent it is shared with, which loses it too — so the admin
can disconnect it and connect again in the destination. A connection the agent
made in the destination before (it is moving back) starts working again.
Grants TO this agent from other agents are untouched: they work while their
owner stays in its app's workspace, wherever the grantee is.
"""
from __future__ import annotations

from apps.agents.move import Plan

from . import providers
from .models import Connection


def _title(provider: str) -> str:
    return getattr(providers.CLASSES.get(provider), "title", provider)


def _describe(conn: Connection) -> str:
    shared = sorted(g.agent.slug for g in conn.grants.all())
    return (f"{_title(conn.provider)} ({conn.account_label or conn.account_id})"
            + (f", shared with {', '.join(shared)}" if shared else ""))


def participant(p: Plan, apply: bool) -> None:  # noqa: ARG001 - nothing is written either way
    agent, source, dest = p.agent, p.source.slug, p.dest.slug
    rows = (Connection.objects.filter(agent=agent).select_related("app")
            .prefetch_related("grants__agent").order_by("provider", "account_label", "pk"))
    stopping = [c for c in rows if c.app.workspace_id == source]
    resuming = [c for c in rows if c.app.workspace_id == dest]
    p.leaves("connections.Connection", len(stopping),
             f"made with '{source}'s connection apps, which stay there")
    if stopping:
        p.breaks("connections", f"{len(stopping)} connection(s) were made with '{source}'s apps and stop "
                                f"working: {'; '.join(_describe(c) for c in stopping)}. Disconnect them and "
                                f"connect again with '{dest}'s apps")
    if resuming:
        p.breaks("connections_resume", f"{len(resuming)} connection(s) made with '{dest}'s apps work again: "
                                       f"{'; '.join(_describe(c) for c in resuming)}")

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

Grants TO this agent are in force only while it is in the lending connection's
app's workspace (`recipients.grants_in_force`), so moving out of it ends them: the
move deletes those grants and records `grant_revoked` (`during=moved`), and once
the move commits prunes each such connection, so the agent's boxes stop holding a
copy (`recipients.prune`). Moving back does not bring them back: the lender shares
again. Grants of a connection made with the destination's apps are untouched.
"""
from __future__ import annotations

from django.db import transaction

from apps.agents.move import Plan

from . import providers, services
from . import recipients as rcpt
from .models import Connection, ConnectionGrant


def _title(provider: str) -> str:
    return getattr(providers.CLASSES.get(provider), "title", provider)


def _describe(conn: Connection) -> str:
    # The provider and how many agents it is shared with — never the account or
    # the grantees' slugs: this text is stored in the destination's `agent.moved_in`
    # Event, and those name things of the source workspace (and others).
    shared = len(conn.grants.all())  # prefetched by the caller
    return _title(conn.provider) + (f", shared with {shared} agent(s)" if shared else "")


def participant(p: Plan, apply: bool) -> None:
    _own(p)
    _lent(p, apply)


def _lent(p: Plan, apply: bool) -> None:
    """Grants to the agent of connections made with apps outside the destination end."""
    agent = p.agent
    grants = list(ConnectionGrant.objects.filter(agent=agent).exclude(connection__app__workspace_id=p.dest.slug)
                  .select_related("connection__app", "connection__agent__workspace", "connection__user")
                  .order_by("connection__provider", "pk"))
    if not grants:
        return
    # Never the lender or the account: this text is stored in the destination's Event.
    p.breaks("connections_lent", f"{len(grants)} connection(s) lent to it stop: "
                                 f"{'; '.join(_title(g.connection.provider) for g in grants)}. Their owners "
                                 f"share them again in '{p.dest.slug}' if they should")
    if not apply:
        return
    conns = [g.connection for g in grants]
    ConnectionGrant.objects.filter(pk__in=[g.pk for g in grants]).delete()
    for conn in conns:
        services.record_grant_revoked(services.Subject.of(conn), agent, by=p.by, during="moved")
    # After the move commits, when the agent is in its new workspace: its boxes
    # no longer qualify, and are dropped from each envelope.
    transaction.on_commit(lambda: [rcpt.prune(c) for c in conns])


def _own(p: Plan) -> None:
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

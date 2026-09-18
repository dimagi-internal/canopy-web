"""The fleet's "waiting on you" items, as an MCP tool.

**Why this exists.** The agent inbox declared its page state by serialising the
ROWS it displayed — id, kind, title, age — because there was no way for an agent
to read an item back. That is the "send the data, not the selection" shape that
`/insights` was moved off: it duplicates a read the server already does, can go
stale between render and send, and becomes a second place the tenant gate could
be got wrong.

Fixing the page without this tool would have been worse than leaving it: strip
the rows and the agent can no longer say anything at all about an item. So the
read moves to the server first, where it belongs (reads go through the server —
see the three doors in `docs/architecture/embedding-a-canopy-agent.md` §7), and
the page is then free to send ids alone.

**The tenant gate is borrowed, not re-written.** `workspace_slugs_for_user_id`
is the one authorizer — `apps/workspaces/services.py` is the only module allowed
to decide who is allowed into a workspace, and `test_workspace_authorizer_is_sole_gate`
fails the build on a second implementation. So this asks it for the caller's
slugs and filters agents by them, exactly as `list_insights` does. An item has
no workspace column of its own; its tenant is one hop away through its agent,
the same derivation `Turn` uses.
"""

from __future__ import annotations

from asgiref.sync import sync_to_async

from apps.mcp.audit import current_user_id, write_audit
from apps.mcp.server import mcp


def _open_items(user_id: int | None, agent_slug: str | None, kind: str | None, limit: int) -> list[dict]:
    """Open items in the caller's visible agent workspaces, newest first.

    No `user_id is None` check here on purpose. The authorizer already returns
    an EMPTY set for an unresolvable caller — its own docstring says so — and an
    empty set filters to nothing. Re-checking would be a second implementation of
    the question `workspace_slugs_for_user_id` exists to be the only answer to,
    and a second implementation is how a predicate drifts.
    """
    from apps.agents.models import Agent
    from apps.harness.models import Item
    from apps.workspaces import services as wsvc

    slugs = wsvc.workspace_slugs_for_user_id(user_id)
    agents = Agent.objects.filter(workspace_id__in=slugs)
    if agent_slug:
        agents = agents.filter(slug=agent_slug)

    qs = (
        Item.objects.filter(agent__in=agents, state=Item.OPEN)
        .select_related("agent")
        .order_by("-created_at")
    )
    if kind:
        qs = qs.filter(kind=kind)

    return [
        {
            "id": str(item.id),
            "agent": item.agent.slug,
            "kind": item.kind,
            "title": item.title,
            # Truncated deliberately: an item body can be a whole review, and a
            # tool result that buries the question is worse than one that
            # points at it.
            "body": (item.body or "")[:600],
            "origin": item.origin,
            "created_at": item.created_at.isoformat(),
        }
        for item in qs[: max(1, min(limit, 100))]
    ]


@mcp.tool
async def list_items(
    agent: str | None = None,
    kind: str | None = None,
    limit: int = 20,
) -> list[dict]:
    """Open items across the fleet — the "waiting on you" queue.

    An Item is work a HUMAN does, as opposed to a Turn, which is work an agent
    does. Returns only OPEN items, newest first, in the agent workspaces the
    caller can see.

    Filters (optional): `agent` slug, `kind` (e.g. review, question).
    `limit` is clamped to 100.

    When a request is about "these", "the ones on screen", or "my inbox", read
    `current_page` first: the page gives you the ids it is showing, and this
    resolves them with the caller's own permissions applied.
    """
    user_id = current_user_id()
    rows = await sync_to_async(_open_items, thread_sensitive=True)(
        user_id, agent, kind, limit
    )
    await write_audit(
        user_id=user_id, tool="list_items",
        args_summary=f"agent={agent} kind={kind} -> {len(rows)}",
    )
    return rows

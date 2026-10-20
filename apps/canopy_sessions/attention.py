"""Chats waiting on a person — the supervisor feed's own rule (`feed.status`),
read through the same list path, so Home and the feed and the phone's pushes
can never disagree about which session needs you."""
from __future__ import annotations

from apps.attention.types import AttentionRow
from apps.workspaces.services import scoped_path

from . import feed, services
from .api import _filtered_sessions, _out


def items(request, *, slugs: set[str]) -> list[AttentionRow]:
    # `_filtered_sessions` scopes by the request's read rule — the same slugs.
    rows = services.with_driving_turn(_filtered_sessions(request, state="active"))
    rows = services.with_open_turn(services.with_last_reply(rows))
    out = []
    for s in rows:
        row = _out(s, reply=True, viewer=request.user)
        if row["feed_status"] != feed.WAITING or row["workspace"] not in slugs:
            continue
        out.append(AttentionRow(
            key=f"session:{row['id']}",
            kind="session",
            reason="Needs an answer" if row["waiting_on_you"] else "Your move",
            title=row["title"] or row["opening"] or "Chat",
            body=(row["last_reply"] or "")[:400],
            workspace=row["workspace"],
            agent_slug=row["agent_slug"] or "",
            project=row["project"] or "",
            url=scoped_path(row["workspace"], f"/chat/{row['id']}"),
            last_activity_at=row["last_activity_at"],
            actions=("open",),
            target={"session": str(row["id"])},
        ))
    return out

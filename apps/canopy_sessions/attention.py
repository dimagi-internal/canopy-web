"""Chats waiting on a person — the supervisor feed's own rule (`feed.status`),
read through the same list path, so Home and the feed and the phone's pushes
can never disagree about which session needs you — and the person's chats that
are running right now, listed apart (`running`) so nothing reads them as asks."""
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
        if row["workspace"] not in slugs:
            continue
        if row["feed_status"] == feed.WAITING:
            reason, running = ("Needs an answer" if row["waiting_on_you"] else "Your move"), False
        elif row["running"] and _yours_running(request.user, s, row):
            reason, running = ("Running on its own" if row["turn_mode"] == "auto" else "Running"), True
        else:
            continue
        out.append(AttentionRow(
            key=f"session:{row['id']}",
            kind="session",
            reason=reason,
            title=row["title"] or row["opening"] or "Chat",
            body=(row["last_reply"] or "")[:400],
            workspace=row["workspace"],
            agent_slug=row["agent_slug"] or "",
            # The folder /ide files it under: the emdash project its worktree lives
            # in (a repo, or the agent's own), so Home groups sessions the same way.
            project=row["emdash_project"] or row["project"] or "",
            url=scoped_path(row["workspace"], f"/chat/{row['id']}"),
            last_activity_at=row["last_activity_at"],
            actions=("open",),
            target={"session": str(row["id"])},
            running=running,
        ))
    return out


def _yours_running(viewer, session, row) -> bool:
    # The feed's own ownership rule (`feed.held`): a colleague's session on their
    # box is theirs to watch. An agent's own auto run counts — it is yours to see
    # moving, just not to answer.
    held = feed.held(viewer, session, turn_mode=row["turn_mode"], turn_origin=getattr(session, "_turn_origin", "") or "")
    return held in ("", feed.AUTO)

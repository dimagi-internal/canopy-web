"""Chats waiting on a person — the supervisor feed's own rule (`feed.status`),
read through the same list path, so Home and the feed and the phone's pushes
can never disagree about which session needs you — and the person's chats that
are running right now, listed apart (`running`) so nothing reads them as asks.

A waiting chat held behind a paused or offline runner is listed as `parked`,
naming the box, rather than dropped: it is still yours, a reply just queues."""
from __future__ import annotations

from apps.attention import types as att
from apps.attention.types import AttentionRow
from apps.workspaces.links import scoped_path

from . import feed
from . import transcript_read
from .api import _filtered_sessions, _out


def items(request, *, slugs: set[str]) -> list[AttentionRow]:
    # `_filtered_sessions` scopes by the request's read rule — the same slugs.
    rows = transcript_read.with_driving_turn(_filtered_sessions(request, state="active"))
    rows = transcript_read.with_open_turn(transcript_read.with_last_reply(rows))
    out = []
    for s in rows:
        row = _out(s, reply=True, viewer=request.user)
        if row["workspace"] not in slugs:
            continue
        said = _state(request.user, s, row)
        if said is None:
            continue
        state, reason = said
        out.append(AttentionRow(
            key=f"session:{row['id']}",
            kind="session",
            state=state,
            reason=reason,
            title=_title(row),
            private=row["sensitive"],
            body=(row["last_reply"] or "")[:400],
            workspace=row["workspace"],
            agent_slug=row["agent_slug"] or "",
            # The folder /ide files it under: the emdash project its worktree lives
            # in (a repo, or the agent's own), so Home groups sessions the same way.
            project=row["emdash_project"] or row["project"] or "",
            url=scoped_path(row["workspace"], f"/sessions/{row['id']}"),
            last_activity_at=row["last_activity_at"],
            actions=("open",),
            target={"session": str(row["id"])},
        ))
    return out


def private_title(row) -> str:
    """How a list names a private conversation: what it is and who started it."""
    who = row.get("started_by_name") or ""
    return f"Private conversation · {who}" if who else "Private conversation"


def _title(row) -> str:
    if row["sensitive"]:
        return private_title(row)
    return row["title"] or row["opening"] or "Chat"


def _state(viewer, session, row) -> tuple[str, str] | None:
    status = row["feed_status"]
    if status == feed.WAITING:
        return (att.ASKING, "Needs an answer") if row["waiting_on_you"] else (att.YOUR_MOVE, "Your move")
    if status == feed.PARKED:
        away = "paused" if row["runner_status"] == "paused" else "offline"
        return att.PARKED, f"Waiting — {row['runner_name'] or 'its runner'} is {away}"
    if row["running"]:
        # The feed's own ownership rule (`feed.held`): a colleague's session on
        # their box is theirs to watch. An agent's own auto run counts — yours to
        # see moving, just not to answer.
        held = feed.held(viewer, session, turn_mode=row["turn_mode"], turn_origin=getattr(session, "_turn_origin", "") or "")
        if held == "":
            return att.RUNNING, "Running"
        if held == feed.AUTO:
            return att.RUNNING_AUTO, "Running on its own"
    return None

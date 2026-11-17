"""Chats waiting on a person — the supervisor feed's own rule (`feed.status`),
read through the same list path, so Home and the feed and the phone's pushes
can never disagree about which session needs you — and the person's chats that
are running right now, listed apart (`running`) so nothing reads them as asks.

A waiting chat held behind a paused or offline runner is listed as `parked`,
naming the box, rather than dropped: it is still yours, a reply just queues.

A chat of yours that nobody is acting on — you spoke last and no reply came,
or your message is queued and no runner has claimed it — is listed as
`stalled` once it has sat `STALLED_AFTER`. The feed rule calls those "not
waiting on you" (the agent owes the reply), which is true, but without this
they were on no list at all: a session never drops off Home without a reason
(someone else's, deliberately paused, or ran on its own)."""
from __future__ import annotations

import datetime as dt

from django.utils import timezone

from apps.attention import types as att
from apps.attention.types import AttentionRow
from apps.workspaces.links import scoped_path

from . import feed, session_state, transcript_read
from .api import _filtered_sessions
from .session_row import session_out

#: How long a session sits with nobody acting on it before Home lists it as
#: stalled — past a runner's claim tick and a slow first token.
STALLED_AFTER = dt.timedelta(minutes=10)


def items(request, *, slugs: set[str]) -> list[AttentionRow]:
    # `_filtered_sessions` scopes by the request's read rule — the same slugs.
    rows = transcript_read.with_driving_turn(_filtered_sessions(request, state="active"))
    rows = session_state.with_state(transcript_read.with_last_reply(rows))
    out = []
    for s in rows:
        row = session_out(s, reply=True, viewer=request.user)
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
        held = feed.held(viewer, session, auto=feed.ran_on_its_own(session))
        if held == "":
            return att.RUNNING, "Running"
        if held == feed.AUTO:
            return att.RUNNING_AUTO, "Running on its own"
        return None
    if status == "":
        return _stalled(viewer, session, row)
    return None


def _stalled(viewer, session, row) -> tuple[str, str] | None:
    """Yours, on a live (or not yet bound) runner, not an auto run, and nobody
    is acting: the feed rule's "the agent owes the reply", gone quiet."""
    if feed.held(viewer, session, auto=feed.ran_on_its_own(session)) != "":
        return None
    if timezone.now() - row["last_activity_at"] < STALLED_AFTER:
        return None
    if session_state.turn_open(session):
        return att.STALLED, "Your message is queued — no runner has picked it up"
    binding = getattr(session, "runner_binding", None)
    if transcript_read.person_spoke_last(session, binding):
        return att.STALLED, "No reply after your message"
    return None

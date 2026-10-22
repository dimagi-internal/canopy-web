"""A chat session's Slack thread, from the API: share a session into Slack,
detach it, move it to a new thread.

Mounted at `/api/canopy-sessions/slack/...` (`apps/api/api.py`), beside the
session routes they act on — same paths and operation ids as when they lived in
`apps/canopy_sessions/api.py` — so canopy_sessions knows nothing of Slack. Which
sessions a caller may name is canopy_sessions' rule (`readable_sessions`), the
same one every session route reads.
"""
from __future__ import annotations

from django.http import HttpRequest
from ninja import Router
from ninja.errors import HttpError

from apps.api.auth import session_auth
from apps.canopy_sessions import access
from apps.canopy_sessions import api as session_api

from . import share
from .schemas import (
    SlackDetachIn,
    SlackDetachOut,
    SlackNewThreadIn,
    SlackNewThreadOut,
    SlackShareIn,
    SlackShareOut,
)

router = Router(auth=session_auth, tags=["chat"])


@router.post("/slack/share", response=SlackShareOut, summary="Post a session's summary to Slack")
def share_session_to_slack(request: HttpRequest, payload: SlackShareIn):
    """Post a summary of the current session into a Slack channel.

    `channel` is a channel name (`#dev`) or id; the canopy app must be in it.
    `summary` is Markdown — what we are doing, why, and where it stands.

    `mode`: `broadcast` (default) — one post; replies in Slack stay between
    people. `bind` — the post starts a thread bound to this session from now
    on, exactly as if the session had been started from Slack: its replies
    appear in the thread and teammates' replies reach the session.

    Sharing a session that is ALREADY bound posts `summary` as an update in its
    thread instead (mode and channel are then ignored). `channel` is required
    otherwise.

    Identify the session with `claude_session_id` ($CLAUDE_CODE_SESSION_ID),
    else `emdash_task` + `emdash_project`, else `session_id`. A broadcast works
    without one; a bind does not. `workspace` is needed only when you belong to
    several workspaces with Slack connected and no session is given.

    An AGENT answering a turn names its conversation with `turn_id`: its own
    login may act on the conversation of a turn it is running, while that turn
    is in flight — post an update, or (after `detach_session_from_slack`) bind
    again, only in the conversation's own channel. To move the conversation to
    a fresh thread in one step, use `start_new_slack_thread`.
    """
    visible = session_api.readable_sessions(request)
    own = False
    if payload.turn_id:
        session, own = share.resolve_turn_session(request.user, payload.turn_id, visible=visible)
        if session is None:
            raise HttpError(404, "no conversation of a live turn of yours matches that turn_id "
                                 "(a turn reaches its conversation only while it is claimed or running)")
    else:
        try:
            session = share.resolve_session(
                request.user, session_id=payload.session_id,
                claude_session_id=payload.claude_session_id,
                emdash_task=payload.emdash_task, emdash_project=payload.emdash_project,
                visible=visible)
        except share.Ambiguous:
            raise HttpError(409, "more than one session matches that emdash task; pass "
                                 "claude_session_id or session_id") from None
    result = share.share_session(request.user, channel=payload.channel, summary=payload.summary,
                                 mode=payload.mode, session=session, workspace=payload.workspace,
                                 own_turn=own)
    if result.status == share.SENSITIVE:
        raise HttpError(403, result.message)
    if not result.ok:
        raise HttpError(422 if result.status == share.BAD_REQUEST else 409, result.message)
    return {"status": result.status, "message": result.message, "mode": payload.mode,
            "channel": result.channel_id, "ts": result.ts, "permalink": result.permalink,
            "session_id": result.session.pk if result.session else None}


@router.post("/slack/detach", response=SlackDetachOut,
             summary="Disconnect a session from its Slack thread")
def detach_session_from_slack(request: HttpRequest, payload: SlackDetachIn):
    """Disconnect a session from the Slack thread it is bound to — the undo of
    sharing it with `bind` (`share_session_to_slack`), and works as well on a
    session Slack itself started.

    From then on the session's replies are no longer posted into the thread, and
    replies in the thread no longer reach the session (they are ignored). The
    thread gets one last note saying it is no longer connected (best-effort; a
    Slack failure does not undo the detach). Nothing already posted is deleted,
    and the binding is kept on the session as history.

    Name the session with `session_id`, else `claude_session_id`
    ($CLAUDE_CODE_SESSION_ID), else `emdash_task` + `emdash_project`. Needs
    owner or editor access to the session. An agent names the conversation it
    is having with `turn_id` instead: its own login may act on the conversation
    of a turn it is running, while that turn is in flight. Idempotent: a session
    with no thread answers `status: not_bound`. To connect it again, share it
    with `bind`, which starts a fresh thread.
    """
    session = _slack_session_for(request, turn_id=payload.turn_id, session_id=payload.session_id,
                                 claude_session_id=payload.claude_session_id,
                                 emdash_task=payload.emdash_task, emdash_project=payload.emdash_project)
    result = share.detach_session(request.user, session)
    return {"status": result.status, "message": result.message, "session_id": session.pk,
            "channel": result.channel_id, "thread_ts": result.thread_ts,
            "notice_posted": result.notice_posted}


def _slack_session_for(request: HttpRequest, *, turn_id: str = "", session_id: str = "",
                       claude_session_id: str = "", emdash_task: str = "", emdash_project: str = ""):
    """The session a Slack-thread write names, with write access proven — or an HttpError.

    `turn_id` first: the agent's own live turn (`access.own_turn_session`) is
    write access to that one conversation and nothing else. Otherwise the
    ordinary rule: a session the caller can open, and write in."""
    visible = session_api.readable_sessions(request)
    if not (turn_id or session_id or claude_session_id or emdash_task):
        raise HttpError(422, "name the session: turn_id, session_id, claude_session_id or emdash_task")
    if turn_id:
        session, own = share.resolve_turn_session(request.user, turn_id, visible=visible)
        if session is None:
            raise HttpError(404, "no conversation of a live turn of yours, or a session you can "
                                 "open, matches that turn")
        if own:
            return session
    else:
        try:
            session = share.resolve_session(
                request.user, session_id=session_id, claude_session_id=claude_session_id,
                emdash_task=emdash_task, emdash_project=emdash_project, visible=visible)
        except share.Ambiguous:
            raise HttpError(409, "more than one session matches that emdash task; pass "
                                 "claude_session_id or session_id") from None
    if session is None:
        raise HttpError(404, "no session you can open matches that")
    if not access.can_write(request.user, session):
        raise HttpError(403, "you can read this session but not act in it")
    return session


@router.post("/slack/new-thread", response=SlackNewThreadOut,
             summary="Move a session's conversation to a new Slack thread in the same channel")
def start_new_slack_thread(request: HttpRequest, payload: SlackNewThreadIn):
    """Start a new thread in the channel this conversation is in, and move the
    conversation there.

    Posts `summary` (Markdown) as a new top-level message in the conversation's
    own Slack channel, binds the session to that message's thread — replies
    there reach the session, the session's replies post there — and tells the
    old thread where it went. Replies in the old thread no longer reach the
    session. The channel is always the conversation's own; to post elsewhere,
    share instead.

    An agent names the conversation it is having with `turn_id` (its caller
    envelope's `turn_id`): its own login may move the conversation of a turn it
    is running, while that turn is in flight, and no other. Anyone else names
    it with `session_id` or `claude_session_id` and needs owner or editor
    access. 409 when the session holds no thread or is a direct message.
    """
    session = _slack_session_for(request, turn_id=payload.turn_id, session_id=payload.session_id,
                                 claude_session_id=payload.claude_session_id)
    if not (payload.summary or "").strip():
        raise HttpError(422, "a summary is required: it is what the new thread opens with")
    result = share.start_new_thread(request.user, session, payload.summary)
    if result.status == share.SENSITIVE:
        raise HttpError(403, result.message)
    if result.status in (share.NOT_BOUND_THREAD, share.IS_DM):
        raise HttpError(409, result.message)
    if not result.ok:
        raise HttpError(422 if result.status == share.BAD_REQUEST else 409, result.message)
    prev = result.previous or {}
    return {"status": result.status, "message": result.message, "session_id": session.pk,
            "channel": result.channel_id, "channel_name": result.channel_name,
            "thread_ts": result.ts, "permalink": result.permalink,
            "previous": {"channel": prev.get("channel") or "", "thread_ts": prev.get("thread_ts") or "",
                         "permalink": prev.get("permalink") or ""}}

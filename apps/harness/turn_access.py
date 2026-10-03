"""Who may read a turn's LOG — its prompt, ledger, raw transcript, caller context.

A turn's existence and status are the interaction tier: any member sees that
the agent ran, when, for which origin, and how it ended, because that is what
"the board" is. Its CONTENT is a log, and a log is the workspace admin's
(`permissions.LOGS_READ`) — before 2026-10-02 every member, viewers included,
read every turn's prompt, transcript and the caller's contact profile, which
on an agent that answers outside contacts is other people's correspondence.

Content is readable by:

* whoever started it (the initiator, or the person who enqueued it);
* the agent's admins (`Agent.is_admin`: its owner, a workspace owner, an
  explicit grant) and the agent's own login — the people operating it;
* the human whose runner claimed it (they ran it on their box);
* a workspace admin or owner (`LOGS_READ`);
* for a chat turn, anyone who may read that chat (`canopy_sessions.access`) —
  and ONLY them: a chat is private to its people, which no role overrides.

Every door asks `can_read_turn_content`: the REST detail routes, the list
redaction, the unclaimable report, and the live turn socket.
"""
from __future__ import annotations

from apps.workspaces import permissions as perms

#: Fields blanked on a turn whose content the caller may not read.
REDACTED_FIELDS = {"prompt": "", "origin_ref": {}, "result_note": "", "share_token": ""}


def _workspace_of(turn) -> str | None:
    if turn.agent_id:
        return turn.agent.workspace_id
    if turn.chat_session_id:
        return turn.chat_session.workspace_id
    return turn.workspace_id


def can_read_turn_content(user, turn, _memo: dict | None = None) -> bool:
    """`_memo` caches the per-workspace and per-agent answers across a list, so
    redacting a page of turns costs a query per agent, not several per row."""
    if not getattr(user, "is_authenticated", False):
        return False
    memo = _memo if _memo is not None else {}
    if turn.chat_session_id:
        from apps.canopy_sessions import access as session_access

        key = ("chat", turn.chat_session_id)
        if key not in memo:
            memo[key] = session_access.can_read(user, turn.chat_session)
        return memo[key]
    if user.pk in (turn.initiator_user_id, turn.enqueued_by_id):
        return True
    if turn.claimed_by_id is not None:
        key = ("box", turn.claimed_by_id)
        if key not in memo:
            memo[key] = turn.claimed_by.owner_id == user.pk
        if memo[key]:
            return True
    ws = _workspace_of(turn)
    if ws:
        key = ("logs", ws)
        if key not in memo:
            memo[key] = perms.can(user, ws, perms.LOGS_READ)
        if memo[key]:
            return True
    if turn.agent_id:
        key = ("agent", turn.agent_id)
        if key not in memo:
            agent = turn.agent
            memo[key] = (agent.user_id is not None and agent.user_id == user.pk) or agent.is_admin(user)
        return memo[key]
    return False


def redact(turns, user):
    """Blank the content of every turn in `turns` the user may not read, in
    memory only, and mark it (`content_hidden`) so a page can say why rather
    than show an empty prompt. Returns the same iterable for chaining."""
    memo: dict = {}
    # Every chat turn's answer in ONE query, through the same chat ACL
    # `can_read_turn_content` asks per row. Per row it was a session load and a
    # read check each — ~3 queries a turn, 2,468 for one page on labs
    # (SLOW_REQUEST, 2026-10-03).
    chat_ids = {t.chat_session_id for t in turns if t.chat_session_id}
    if chat_ids and getattr(user, "is_authenticated", False):
        from apps.canopy_sessions import access as session_access

        readable = session_access.readable_ids(user, chat_ids)
        for sid in chat_ids:
            memo[("chat", sid)] = sid in readable
    for t in turns:
        if can_read_turn_content(user, t, memo):
            t.content_hidden = False
            continue
        for field, empty in REDACTED_FIELDS.items():
            setattr(t, field, type(empty)())
        t.content_hidden = True
    return turns

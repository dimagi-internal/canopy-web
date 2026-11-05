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

A turn raised from a task in a PERSONAL project is read only by that project's readers
(`agents.services.may_read_project`): none of the people above reach past it, because
its prompt carries the project. This is not the sensitivity mechanism, which is about
what a runner may keep and would ground the turn on sensitive-data boxes; and it covers
the turn's log here, not a session the turn ran in (`canopy_sessions.access` decides that).

A SENSITIVE turn's content is nobody's through these doors (`harness.sensitivity`):
its words belong to its conversation, which its own people read on the chat page,
and every door here is a log a review or another AI could read.

Every door asks `can_read_turn_content`: the REST detail routes, the list
redaction, the unclaimable report, and the live turn socket.
"""
from __future__ import annotations

from apps.workspaces import permissions as perms

from . import sensitivity

#: Fields blanked on a turn whose content the caller may not read. `provenance`
#: too: which token (by its label), user agent and request id made a turn is a
#: log, so it is read by the same people as the prompt.
REDACTED_FIELDS = {"prompt": "", "origin_ref": {}, "result_note": "", "share_token": "",
                   "provenance": {}}
#: Blanked as well on a hidden turn of a personal project: a report is the board's
#: everywhere else, and here it is the project's words.
PERSONAL_REDACTED_FIELDS = {"report_title": "", "report_summary": "", "session_slug": "", "task_ext_ids": [],
                            "work_product_urls": []}



def _personal_project(turn, memo: dict):
    """The personal project `turn` was raised from, or None."""
    if not turn.raised_from_task_id:
        return None
    key = ("personal", turn.raised_from_task_id)
    if key not in memo:
        task = turn.raised_from_task
        memo[key] = task.project if task.project_id and task.project.is_personal else None
    return memo[key]


def on_personal_project(turn) -> bool:
    """Was `turn` raised from a task in a personal project?"""
    return _personal_project(turn, {}) is not None


def can_read_turn_content(user, turn, _memo: dict | None = None) -> bool:
    """`_memo` caches the per-workspace and per-agent answers across a list, so
    redacting a page of turns costs a query per agent, not several per row."""
    if sensitivity.is_sensitive(turn):
        return False
    return can_read_turn_log(user, turn, _memo)


def can_read_caller_context(user, turn) -> bool:
    """Who may read a turn's caller envelope: whoever may read its content, and the agent
    itself, from the login canopy-web issued for THIS turn while it runs (never another
    turn, a finished one, a stored token, or a sensitive conversation's). The envelope is
    the document the runner already put in that agent's hands at the claim (who asked,
    verified, role, permissions, turn_mode, what canopy knows of the asker) — no prompt,
    no transcript — and an agent's own login cannot read a person's chat, so without this
    the mid-turn re-read `who_is_asking` documents could never answer there."""
    return can_read_turn_content(user, turn) or is_own_running_turn(user, turn)


def is_own_running_turn(user, turn) -> bool:
    """`user` is the agent's own login, making this request with the per-turn login
    canopy-web issued for `turn`, which is running now, and it is not sensitive."""
    if sensitivity.is_sensitive(turn):
        return False
    from . import turn_asker  # noqa: PLC0415 — as the lazy imports beside it in this package

    running = turn_asker.serving_turn(user)
    return running is not None and running.pk == turn.pk


def can_read_turn_log(user, turn, _memo: dict | None = None) -> bool:
    """Who may read a turn's LOG — the people listed above — whether or not the turn
    is sensitive. Ask it only where what is served holds no words: a sensitive
    turn's stored ledger is metadata (`sensitivity.redact_payload`), so its people
    can still see what the turn did, when, with which tools, and how it ended."""
    if not getattr(user, "is_authenticated", False):
        return False
    memo = _memo if _memo is not None else {}
    project = _personal_project(turn, memo)
    if project is not None:
        from apps.agents.services import may_read_project  # noqa: PLC0415 — agents imports this package

        return may_read_project(user, project)
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
    ws = turn.tenant_slug
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
            memo[key] = agent.is_own_login(user) or agent.is_admin(user)
        return memo[key]
    return False


def redact(turns, user):
    """Blank the content of every turn in `turns` the user may not read, in
    memory only, and mark it (`content_hidden`) so a page can say why rather
    than show an empty prompt. Returns the same iterable for chaining."""
    memo: dict = {}
    # A page of turns reads their personal projects once, not per row.
    task_ids = {t.raised_from_task_id for t in turns if t.raised_from_task_id}
    if task_ids:
        from apps.agents.models import AgentTask  # noqa: PLC0415 — agents imports this package

        memo.update({("personal", t.pk): (t.project if t.project_id and t.project.is_personal else None)
                     for t in AgentTask.objects.filter(pk__in=task_ids).select_related("project__agent")})
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
        if _personal_project(t, memo) is not None:
            for field, empty in PERSONAL_REDACTED_FIELDS.items():
                setattr(t, field, type(empty)())
            t.raised_from_task = None  # its alias is the agent's name for the work
        t.content_hidden = True
    return turns

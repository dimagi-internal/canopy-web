"""Project chats: a chat with no agent, only a repo follows the repo-turn order plus one rule (`project_session_allows` / `project_session_covers`). Import these names from `routing`."""
from __future__ import annotations

from apps.harness import runner_requirements as rr

from .models import (
    Runner,
    Turn,
)
from .routing_orders import _cascade_allows, order_for

# ---- project chats: a chat with no agent, only a repo ----
#
# Its UNBOUND turns follow the rules a repo turn follows, plus one: with no
# workspace order, only a box the chatting person administers. (Otherwise any
# session-capable box in the tenant would take them, and a teammate's new project chat
# would land on someone else's laptop.) A bound session still claims only on its
# holder, and a pin still bypasses all of this. The claim asks
# `project_session_allows`; every reader that mirrors it (`claim._refined_allows`,
# so turn_reach, the unclaimable report and the topology) asks
# `project_session_covers` — the same predicates without the clock.

def is_project_session_turn(turn: Turn) -> bool:
    """A chat turn with no agent whose session is not bound to a runner — the
    turns the project-chat rules route. A chat with neither an agent nor a repo
    counts: it has no repo to match, but it is still somebody's work."""
    if not turn.chat_session_id or turn.chat_session.agent_id:
        return False
    binding = getattr(turn.chat_session, "runner_binding", None)
    return binding is None or binding.runner_id is None


def project_session_rows(turn: Turn, orders: dict, *, requires: frozenset | None = None) -> list | None:
    """The workspace order a project chat's turn follows (`order_for` on its
    session's workspace and repo), or None when the workspace has none."""
    sess = turn.chat_session
    reqs = rr.requirements_of(turn) if requires is None else requires
    return order_for(sess.workspace_id, sess.project, orders, requires=reqs, sessions=True)


def _chatter_administers(runner: Runner, turn: Turn, admins: dict | None) -> bool:
    """Rule 3: the person who sent it (`enqueued_by`) administers this box. A
    turn with no person behind it gets no box this way. Cached per (box, person)
    when the caller passes `admins`."""
    if not turn.enqueued_by_id:
        return False
    from .runners import can_administer_runner

    if admins is None:
        return can_administer_runner(turn.enqueued_by, runner)
    key = (runner.id, turn.enqueued_by_id)
    if key not in admins:
        admins[key] = can_administer_runner(turn.enqueued_by, runner)
    return admins[key]


def project_session_covers(runner: Runner, turn: Turn, orders: dict, *,
                           requires: frozenset | None = None, admins: dict | None = None) -> bool:
    """Could this runner EVER take this unbound project-chat turn? Rules 1–3,
    without the cascade's clock (membership, as for a repo turn's order)."""
    if runner.directed_only():
        return False
    project = turn.chat_session.project
    if project and not runner.offers_project_in(project, turn.chat_session.workspace_id):
        return False  # 1. the box holds the repo, for this workspace
    rows = project_session_rows(turn, orders, requires=requires)
    if rows is not None:
        return any(r.id == runner.id for r in rows)  # 2. the workspace shares it
    return _chatter_administers(runner, turn, admins)  # 3. or you run it


def project_session_allows(runner: Runner, turn: Turn, orders: dict, now, *,
                           admins: dict | None = None) -> bool:
    """`project_session_covers`, then the order's availability cascade and grace."""
    if not project_session_covers(runner, turn, orders, admins=admins):
        return False
    rows = project_session_rows(turn, orders)
    return rows is None or _cascade_allows(runner, rows, turn, now)


def project_session_waits_why(turn: Turn, orders: dict) -> str:
    """Why no box will take this unbound project-chat turn, in the words of the
    rules above — the reason `unclaimable_queued_turns` and `turn_reach` give."""
    sess = turn.chat_session
    ws = sess.workspace_id
    repo = sess.project
    with_repo = f"with {repo}" if repo else "that takes chats"
    if project_session_rows(turn, orders) is not None:
        return f"the runner order in {ws} lists no session-capable box {with_repo}"
    if not turn.enqueued_by_id:
        return (f"no runner order in {ws}, and this project chat has no person behind "
                "it, so it needs one")
    who = turn.enqueued_by.email or turn.enqueued_by.get_username()
    return f"no runner order in {ws}, and {who} administers no box {with_repo}"

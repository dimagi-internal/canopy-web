"""Push the shared turn status onto a chat session's socket.

canopy's half of `harness.turn_status`: Slack renders that state to mrkdwn and
a button, this renders it to a frame the chat kit draws. Same eleven states,
same derivation, so a thread and a chat page can never disagree about whether
anything is happening.

Why a frame at all, when the client already knows it pressed send: because the
three states a client cannot possibly infer are the three that matter most —
its runner is offline, no runner is set up for this agent, its runner died
mid-turn. The web used to guess at the first by pulling the whole fleet list
and matching the runner BY NAME, which failed open whenever `GET /runners/`
omitted a retired runner; the embedded widget did not guess at all and simply
showed an empty panel until a reply arrived, which on an offline runner is
never.

The frame carries the whole status rather than a delta. It is small, it is
recomputed from rows on every send anyway, and a delta would need the client to
hold a correct prior — which a client that just connected does not have.
"""
from __future__ import annotations

import logging

from apps.realtime.groups import publish, session_group

logger = logging.getLogger(__name__)


def status_for_session(session) -> dict | None:
    """The status of the ask this session is currently waiting on, or None.

    The session's LATEST turn, not its latest unfinished one: a finished turn is
    the honest answer to "what happened to what I sent" right up until something
    newer replaces it, and suppressing it would blank the line the moment a
    reply landed — exactly when a reader looks at it.
    """
    from apps.harness import turn_status as ts
    from apps.harness.models import Turn

    turn = (Turn.objects.select_related("agent", "claimed_by", "pinned_runner",
                                        "chat_session", "chat_session__agent")
            .filter(chat_session_id=session.pk).order_by("-created_at").first())
    if turn is None:
        return None
    return ts.resolve(turn).as_dict()


def publish_for_turn(turn) -> None:
    """Fan this turn's status to everyone watching its session.

    Never raises. A status is an enhancement to a conversation that works
    without it, and this runs inside the runner's append path — the same rule
    `realtime.groups.publish` already follows.
    """
    if not turn.chat_session_id:
        return
    try:
        from apps.harness import turn_status as ts

        payload = ts.resolve(turn).as_dict()
    except Exception:  # noqa: BLE001 — never break an append over a status line
        logger.exception("could not derive turn status for %s", turn.pk)
        return
    publish(session_group(turn.chat_session_id),
            {"type": "session.turn_status", "status": payload})


def publish_for_session(session_id) -> None:
    """Re-derive and fan out from a session id alone — the sweep's entry point,
    where the trigger is another runner's report rather than this turn moving."""
    from apps.harness.models import Turn

    turn = (Turn.objects.select_related("agent", "claimed_by", "pinned_runner",
                                        "chat_session", "chat_session__agent")
            .filter(chat_session_id=session_id).order_by("-created_at").first())
    if turn is not None:
        publish_for_turn(turn)


def sweep() -> int:
    """Re-push the status of every session holding an unfinished turn.

    The clock for the one change that produces no event at all: a runner whose
    laptop closes simply stops heartbeating, and a dead runner cannot report its
    own death. So this rides `sessions_reported` — every OTHER runner's ~10s
    report — exactly as Slack's own sweep does, and for the same reason.

    Scoped to non-terminal turns: a settled status cannot go stale.
    """
    from apps.harness.models import Turn

    session_ids = (Turn.objects.filter(chat_session__isnull=False,
                                       status__in=list(Turn.NON_TERMINAL))
                   .values_list("chat_session_id", flat=True).distinct())
    n = 0
    for sid in session_ids:
        try:
            publish_for_session(sid)
        except Exception:  # noqa: BLE001 — one bad session must not stop the rest
            logger.exception("turn-status sweep failed for session %s", sid)
        n += 1
    return n

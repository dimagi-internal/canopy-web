"""Push policy. The only place that decides WHETHER to push.

The trigger problem: the fleet's waiting set is a COUNT (open items per agent),
not a single event, so nothing naturally emits "the fleet needs you now". We
snapshot each agent's open-item count and push only when it goes UP.
"""
from __future__ import annotations

import datetime as dt
import json
import logging

from django.conf import settings
from django.db import connection, transaction
from django.utils import timezone
from pywebpush import WebPushException, webpush

from apps.agents.models import Agent
from apps.canopy_sessions.models import Message, Session
from apps.harness.models import Item, Turn

from .models import AgentWaitingSnapshot, PushSubscription, session_idle_minutes_for

logger = logging.getLogger(__name__)


def _dirty_set() -> set[int]:
    """The agents marked in THIS connection's current transaction.

    Lives on the connection, not the module: connections are thread-local
    (django/utils/connection.py:41) while a module global is not, so two
    concurrent requests shared one set — and whichever committed first drained
    BOTH, recomputing the other thread's agent on a connection that could not
    yet see its uncommitted rows. That agent's push was silently dropped. Per
    connection is at most one transaction's worth plus any residue from a
    rolled-back transaction, which _flush recomputes harmlessly because
    refresh_agent_waiting re-reads the truth from the DB and pushes only on
    an increase.
    """
    if not hasattr(connection, "_push_dirty"):
        connection._push_dirty = set()
    return connection._push_dirty


def _send_one(sub: PushSubscription, payload: dict) -> None:
    """The raw send. Patched in tests — keep it dependency-free and dumb."""
    webpush(
        subscription_info={
            "endpoint": sub.endpoint,
            "keys": {"p256dh": sub.p256dh, "auth": sub.auth},
        },
        data=json.dumps(payload),
        vapid_private_key=settings.VAPID_PRIVATE_KEY,
        vapid_claims={"sub": settings.VAPID_SUBJECT},
        timeout=10,  # pywebpush's own default is dead code: send() pops a key that is
                     # always present, so None reaches requests.post. Unbounded here
                     # would hold a request thread forever — and the bare except below
                     # cannot catch a hang.
    )


def send_to_user(user, title: str, body: str, url: str, count: int | None = None) -> int:
    """Push to every browser this user has registered. Returns sends that stuck.

    A subscription dies silently when the app is uninstalled — the push service
    starts returning 404/410. That is the only reliable signal we get, so we
    prune on it. Any other failure is the service's problem, not the
    subscription's: count it and keep the row.

    `count` (optional) rides along in the payload so the service worker's
    `push` listener can set the app-icon badge from a push that arrives while
    the app is closed — SupervisorPage.setBadge only runs on mount, so without
    this the badge goes stale until the app is next opened.
    """
    if not settings.VAPID_PRIVATE_KEY:
        return 0  # push not configured — stay silent rather than raise
    payload = {"title": title, "body": body, "url": url}
    if count is not None:
        payload["count"] = count
    sent = 0
    for sub in list(user.push_subscriptions.all()):
        try:
            _send_one(sub, payload)
            sent += 1
        except WebPushException as exc:
            status = getattr(getattr(exc, "response", None), "status_code", None)
            if status in (404, 410):
                logger.info("push: pruning dead subscription %s (%s)", sub.pk, status)
                sub.delete()
            else:
                PushSubscription.objects.filter(pk=sub.pk).update(
                    failure_count=sub.failure_count + 1
                )
                logger.warning("push: send failed sub=%s status=%s: %s", sub.pk, status, exc)
        except Exception:  # noqa: BLE001
            logger.exception("push: unexpected send failure sub=%s", sub.pk)
    return sent


def refresh_agent_waiting(agent: Agent) -> int:
    """Recompute this agent's waiting_count, push if it went UP, store it.

    Returns the number of pushes sent. The snapshot advances even when nobody is
    subscribed — otherwise the first push after subscribing would fire for items
    that were already sitting there.
    """
    count = Item.objects.filter(agent=agent, state=Item.OPEN).count()
    snap, created = AgentWaitingSnapshot.objects.get_or_create(agent=agent)
    previous = 0 if created else snap.waiting_count
    if count != previous:
        snap.waiting_count = count
        snap.save(update_fields=["waiting_count", "updated_at"])
    if count <= previous:
        return 0  # cleared or unchanged — silence
    owner = getattr(agent, "owner", None)
    if owner is None:
        return 0
    delta = count - previous
    return send_to_user(
        owner,
        title=f"{agent.name} needs you",
        body=f"{delta} new item{'s' if delta != 1 else ''} · {count} waiting",
        url="/supervisor",
        count=count,
    )


def _flush() -> None:
    """Recompute every agent touched in the just-committed transaction, once."""
    dirty = _dirty_set()
    ids = set(dirty)
    dirty.clear()
    for agent in Agent.objects.filter(id__in=ids):
        try:
            refresh_agent_waiting(agent)
        except Exception:  # noqa: BLE001
            # A push must never break the request that triggered it.
            logger.exception("push: refresh failed for agent=%s", agent.slug)


def mark_dirty(agent_id: int) -> None:
    """Note that an agent's waiting set may have changed.

    Registers the flush unconditionally. Redundant callbacks are free: the first
    one to run drains the set and does the work, and the rest find it empty and
    no-op — so a bulk sync of N rows is still ONE recompute per agent.

    Do NOT re-add a `if not _dirty_set()` guard around the registration. Django
    discards on_commit callbacks when a transaction rolls back, but this set is
    not transactional and keeps its entries — so the guard would see a non-empty
    set forever after the first rollback, never register again, and silently
    kill push process-wide until restart.
    """
    _dirty_set().add(agent_id)
    transaction.on_commit(_flush)


# --- A blocked agent asking a question ---------------------------------------
#
# A SECOND producer, deliberately not routed through the item snapshot above.
#
# The snapshot exists because the waiting set is a COUNT with no natural event.
# This is the opposite shape: an agent going from "working" to "waiting on a
# human" is a discrete edge, observed once, and the notification can carry the
# actual question rather than a tally. It is also not an `Item` and must not
# become one — `Item`'s decisions are implement/skip/defer and `implement`
# dispatches a Turn, whereas answering a dialog is a KEYSTROKE into a live
# session. An inbox row whose buttons enqueue a turn would be wrong in a way
# that runs code.
#
# Why it matters at all: rendering the menu perfectly still requires somebody to
# open the app. `spark` sat blocked for 52 minutes on 2026-07-31 with nobody
# looking, and no amount of UI fixes that.

QUESTION_BODY_MAX = 140


def _question_audience(session):
    """Who should be told this session is waiting, or None.

    The agent's owner when there is one; otherwise the human who PAIRED the
    runner — the person whose laptop the session is actually sitting on. A
    runner-discovered session (what `spark` was) has no agent, so without the
    second leg the case that motivated this would notify nobody.

    Fails closed on None, the same way `runner.paired_by` gates tenancy: with
    nobody identifiable, we stay silent rather than broadcast a workspace.
    """
    agent = getattr(session, "agent", None)
    owner = getattr(agent, "owner", None) if agent is not None else None
    if owner is not None:
        return owner
    binding = getattr(session, "runner_binding", None)
    runner = getattr(binding, "runner", None) if binding is not None else None
    return getattr(runner, "paired_by", None) if runner is not None else None


def notify_session_question(session, menu: dict) -> int:
    """Push "this agent is asking you something", deep-linked to the answer.

    The URL is the CHAT, not `/supervisor`: the whole point is that the tap
    lands on the buttons. Sending someone to a dashboard to hunt for which
    session it was is the same delay this exists to remove, just shorter.

    Best-effort — a push must never cost the session report that triggered it.
    """
    if not menu:
        return 0
    user = _question_audience(session)
    if user is None:
        return 0
    question = str(menu.get("question") or "").strip() or "a question"
    if len(question) > QUESTION_BODY_MAX:
        question = question[: QUESTION_BODY_MAX - 1].rstrip() + "…"
    name = (session.title or "").strip() or "An agent"
    try:
        return send_to_user(
            user,
            title=f"{name} is asking",
            body=question,
            url=f"/w/{session.workspace_id}/chat/{session.id}",
        )
    except Exception:  # noqa: BLE001 — never let a notification break the report
        logger.exception("push: session-question notify failed for %s", session.pk)
        return 0


# ---- "your chat is done" ---------------------------------------------------
#
# A session never "ends" — an agent answers, goes quiet, and may pick up again
# a minute later. So the default is NOT a push per finished turn (an agent's
# back-to-back turns would buzz once each) but one push once the session has
# stayed quiet for the recipient's `session_idle_minutes` (default 5). A turn
# ending stamps `Session.finish_push_due_at`; the next turn clears it; the
# runner heartbeat drains whatever falls due. A session flagged
# `notify_every_completion` skips the wait and pushes on every finished turn.
#
# CANCELLED and MISSED never push: a cancel is the human saying stop, and they
# already know.

FINISH_BODY_MAX = 140
FINISH_PUSH_BATCH = 50
_PUSHABLE = (Turn.DONE, Turn.FAILED)
_OPEN = (Turn.QUEUED, Turn.CLAIMED, Turn.RUNNING, Turn.NEEDS_HUMAN)


def _finish_audience(turn: Turn):
    """Who asked for this turn; else whoever a question on this session would go to."""
    return turn.initiator_user or turn.enqueued_by or _question_audience(turn.chat_session)


def _finish_body(session: Session, turn: Turn) -> str:
    reply = (
        Message.objects.filter(session=session, role=Message.ASSISTANT)
        .exclude(plaintext="")
        .order_by("-turn_index")
        .values_list("plaintext", flat=True)
        .first()
    )
    text = " ".join((reply or turn.result_note or "").split())
    if not text:
        return "Reply ready" if turn.status == Turn.DONE else "The turn failed"
    if len(text) > FINISH_BODY_MAX:
        text = text[: FINISH_BODY_MAX - 1].rstrip() + "…"
    return text


def _send_finish(session: Session, turn: Turn, user) -> int:
    """Deep-linked to the chat itself — the tap lands on the reply."""
    name = (session.title or "").strip() or "Your chat"
    title = f"{name} failed" if turn.status == Turn.FAILED else f"{name} is done"
    try:
        return send_to_user(
            user, title=title, body=_finish_body(session, turn),
            url=f"/w/{session.workspace_id}/chat/{session.id}",
        )
    except Exception:  # noqa: BLE001 — a notification must never break its caller
        logger.exception("push: session-finish notify failed for %s", session.pk)
        return 0


def on_session_turn_finished(turn: Turn) -> None:
    """Called by `finish_turn` for a session turn that just reached a terminal state."""
    if not turn.chat_session_id or turn.status not in _PUSHABLE:
        return
    session = turn.chat_session
    user = _finish_audience(turn)
    if user is None:
        return
    if session.notify_every_completion:
        Session.objects.filter(pk=session.pk).update(finish_push_due_at=None)
        transaction.on_commit(lambda: _send_finish(session, turn, user))
        return
    minutes = session_idle_minutes_for(user)
    due = timezone.now() + dt.timedelta(minutes=minutes) if minutes > 0 else None
    Session.objects.filter(pk=session.pk).update(finish_push_due_at=due)


def cancel_session_finish_push(session_id) -> None:
    """A new turn: the session was not done after all."""
    Session.objects.filter(pk=session_id, finish_push_due_at__isnull=False).update(
        finish_push_due_at=None
    )


def send_due_session_pushes(now=None) -> int:
    """Send every "gone quiet" push that has fallen due. Returns pushes sent.

    Every runner heartbeats, so several can drain at once: each row is claimed by
    a conditional UPDATE on the exact due time it was read with, and only the
    runner whose update lands sends.
    """
    now = now or timezone.now()
    due = list(
        Session.objects.filter(finish_push_due_at__lte=now)
        .order_by("finish_push_due_at")
        .values_list("pk", "finish_push_due_at")[:FINISH_PUSH_BATCH]
    )
    sent = 0
    for pk, due_at in due:
        if not Session.objects.filter(pk=pk, finish_push_due_at=due_at).update(finish_push_due_at=None):
            continue  # another runner took it, or a new turn cleared it
        session = (
            Session.objects.select_related("runner_binding").filter(pk=pk).first()
        )
        if session is None or session.status != Session.ACTIVE:
            continue
        if Turn.objects.filter(chat_session=session, status__in=_OPEN).exists():
            continue  # busy again — its own finish will re-arm this
        binding = getattr(session, "runner_binding", None)
        if binding is not None and binding.pending_question:
            continue  # waiting on a human: the question push already said so
        turn = (
            Turn.objects.filter(chat_session=session, finished_at__isnull=False)
            .order_by("-finished_at")
            .first()
        )
        if turn is None or turn.status not in _PUSHABLE:
            continue
        user = _finish_audience(turn)
        if user is not None:
            sent += _send_finish(session, turn, user)
    return sent

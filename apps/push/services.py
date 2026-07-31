"""Push policy. The only place that decides WHETHER to push.

The trigger problem: the fleet's waiting set is a COUNT (open items per agent),
not a single event, so nothing naturally emits "the fleet needs you now". We
snapshot each agent's open-item count and push only when it goes UP.
"""
from __future__ import annotations

import json
import logging

from django.conf import settings
from django.db import connection, transaction
from pywebpush import WebPushException, webpush

from apps.agents.models import Agent
from apps.harness.models import Item

from .models import AgentWaitingSnapshot, PushSubscription

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

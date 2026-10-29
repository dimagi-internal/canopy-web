"""One turn per mail thread at a time: coalescing for `inbound_mail` triggers.

A CloudWatch alarm re-notifies on its thread every evaluation period. Before this,
every new message was a full turn: thread 1a184f500cd30f72 (2026-10-29) ran six in
twelve hours as it grew to 14 messages, each re-reading the procedure, rebuilding
the storm and adding one more GitHub comment. The runner's own per-process memory
(`inbox._alarm_incident_is_owned`) cannot see the other runners, so this is decided
here, under the thread session's row lock.

For mail that fires an agent's `inbound_mail` trigger, per (agent, thread) — the
thread's session:

* a turn on the thread is QUEUED → the mail is folded into it: its `origin_ref`
  takes the newest message (`message_id`, `message_count`, …) and
  `coalesced_messages` counts what it absorbed. No new turn.
* a turn is RUNNING (claimed / running / needs_human) and none queued → ONE
  follow-up is enqueued as usual; later mail folds into it (above).
* nothing live, and a turn on the thread finished less than the trigger's
  `mail_quiet_minutes` ago → the mail is HELD (`PendingMail`); further mail joins
  it; when the window ends (measured from that finish, so a storm that never goes
  quiet still gets its follow-up) one turn starts carrying all of it
  (`release_due`, a tick).
* otherwise — the first message of a thread, or mail after the window — a turn
  starts now.

A message seen twice (push and poll, two runners) is recognised by its
idempotency key and counted once. Mail that fires no trigger is never coalesced:
a person writing twice is two asks.
"""
from __future__ import annotations

import logging
from datetime import timedelta

from django.db import transaction
from django.utils import timezone

from apps.canopy_sessions.models import Session

from .models import PendingMail, Turn

logger = logging.getLogger(__name__)

#: What a newer message replaces on the turn it is folded into: the message itself.
#: Never `from`: the turn's asker was resolved from its first message.
NEWEST_MESSAGE_FIELDS = ("message_id", "message_count", "subject", "discovered_by",
                         "headers", "to", "cc", "inbox_row")

#: How many folded idempotency keys a turn remembers (a storm is dozens, not thousands).
KEYS_KEPT = 200

LIVE = (Turn.CLAIMED, Turn.RUNNING, Turn.NEEDS_HUMAN)


def _keys(ref) -> list:
    return list((ref or {}).get("coalesced_keys") or [])


def _fold_into(turn: Turn, origin_ref: dict, idempotency_key: str, count: int = 1,
               keys: list | None = None) -> Turn:
    ref = dict(turn.origin_ref or {})
    for field in NEWEST_MESSAGE_FIELDS:
        if field in origin_ref:
            ref[field] = origin_ref[field]
    ref["coalesced_messages"] = int(ref.get("coalesced_messages") or 0) + count
    ref["coalesced_keys"] = (_keys(ref) + list(keys or []) + [idempotency_key])[-KEYS_KEPT:]
    Turn.objects.filter(pk=turn.pk).update(origin_ref=ref)
    turn.origin_ref = ref
    return turn


def _already_seen(session, idempotency_key: str):
    """The turn a re-posted message was already folded into or held for, or None."""
    for turn in Turn.objects.filter(chat_session=session).order_by("-created_at")[:20]:
        if idempotency_key in _keys(turn.origin_ref):
            return turn
    held = PendingMail.objects.filter(session=session).first()
    if held is not None and idempotency_key in (held.keys or []):
        return _answer(session)
    return None


def _answer(session):
    """The turn a held message is answered with: the thread's last turn."""
    return _last_ran(session) or Turn.objects.filter(chat_session=session).order_by("-created_at").first()


def _last_ran(session):
    return (Turn.objects.filter(chat_session=session, status__in=list(Turn.TERMINAL),
                                started_at__isnull=False, finished_at__isnull=False)
            .order_by("-finished_at").first())


def fold(trigger, session, *, idempotency_key: str, origin_ref: dict, replay: dict,
         now=None) -> tuple[Turn | None, dict]:
    """Decide what a trigger-fired mail on `session`'s thread does, inside the
    enqueue's transaction. Returns `(answer, carry)`:

    * `answer` not None — no new turn: the Turn it was folded into (or, when held,
      the thread's last turn, which is what the caller is told).
    * `answer` None — enqueue a turn now, adding `carry` to its origin_ref (what a
      held batch it supersedes had gathered).

    `replay` is what `release_due` re-enqueues with: the caller's raw arguments."""
    now = now or timezone.now()
    Session.objects.select_for_update().filter(pk=session.pk).first()
    seen = _already_seen(session, idempotency_key)
    if seen is not None:
        return seen, {}
    on_thread = Turn.objects.filter(chat_session=session)
    queued = (on_thread.filter(status=Turn.QUEUED, origin=Turn.ORIGIN_EMAIL)
              .order_by("-created_at").first())
    if queued is not None:
        return _fold_into(queued, origin_ref, idempotency_key), {}
    held = PendingMail.objects.filter(session=session).first()
    if on_thread.filter(status__in=LIVE).exists():
        # One follow-up behind the running turn, carrying anything held.
        return None, _release_carry(held)
    window = timedelta(minutes=int(getattr(trigger, "mail_quiet_minutes", 0) or 0))
    last = _last_ran(session)
    if held is not None:
        answer = _answer(session)
        if answer is None:   # nothing left to answer with (retention): run it now
            return None, _release_carry(held)
        held.count += 1
        held.keys = (list(held.keys or []) + [idempotency_key])[-KEYS_KEPT:]
        held.payload = replay
        held.save(update_fields=["count", "keys", "payload"])
        return answer, {}
    if window and last is not None and last.finished_at + window > now:
        held = PendingMail.objects.create(session=session, trigger=trigger, payload=replay,
                                          keys=[idempotency_key], due_at=last.finished_at + window)
        logger.info("mail on thread %s held until %s (quiet window)", session.pk, held.due_at)
        return last, {}
    return None, {}


def _release_carry(held) -> dict:
    """Delete a held batch now superseded by a turn being enqueued; what it carried."""
    if held is None:
        return {}
    carry = {"coalesced_messages": held.count, "coalesced_keys": list(held.keys or [])}
    held.delete()
    return carry


def release_due(now=None) -> int:
    """Start the one turn each held thread is owed once its quiet window is over.
    Returns how many turns it enqueued."""
    from . import services  # noqa: PLC0415 — cycle: services -> mail_coalesce

    now = now or timezone.now()
    started = 0
    for pk in list(PendingMail.objects.filter(due_at__lte=now).values_list("pk", flat=True)):
        try:
            with transaction.atomic():
                held = PendingMail.objects.select_for_update(of=("self",)).filter(pk=pk).select_related(
                    "session", "session__agent").first()
                if held is None:
                    continue
                session = held.session
                on_thread = Turn.objects.filter(chat_session=session)
                if on_thread.filter(status__in=list(Turn.NON_TERMINAL)).exists():
                    queued = (on_thread.filter(status=Turn.QUEUED, origin=Turn.ORIGIN_EMAIL)
                              .order_by("-created_at").first())
                    if queued is not None:
                        _fold_into(queued, held.payload.get("origin_ref") or {},
                                   (held.keys or [""])[-1], count=held.count,
                                   keys=list(held.keys or [])[:-1])
                        held.delete()
                    continue   # something else holds the thread; its end lets this run
                if session.agent_id is None:
                    held.delete()
                    continue
                p = held.payload or {}
                ref = {**(p.get("origin_ref") or {}),
                       "coalesced_messages": max(held.count - 1, 0),
                       "coalesced_keys": list(held.keys or [])[:-1]}
                if not ref["coalesced_messages"]:
                    ref.pop("coalesced_messages")
                    ref.pop("coalesced_keys")
                held.delete()
                services.enqueue_turn(
                    # Mail names its own asker (`channels.email.arrive`), as on the way in.
                    agent=session.agent, origin=Turn.ORIGIN_EMAIL, initiator=None,
                    idempotency_key=p.get("idempotency_key") or f"mail-held:{pk}",
                    prompt=p.get("prompt") or "", origin_ref=ref,
                    routing=p.get("routing") or Turn.PREFER_LOCAL,
                    authserv_id=p.get("authserv_id"), coalesce=False,
                    provenance_extra={"released_from": "mail quiet window"},
                )
                started += 1
        except Exception:  # noqa: BLE001 — one thread's failure must not hold the others
            logger.exception("could not release held mail %s", pk)
    return started

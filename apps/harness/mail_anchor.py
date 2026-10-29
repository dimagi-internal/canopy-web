"""A mail thread's ANCHOR session retires on its own once nothing on it is live.

Every inbound email thread gets one canopy Session (`session_sync.email_thread_session`)
and each mail on it becomes a turn whose `chat_session` is that session. The runner
that claims such a turn does not run IN it: it keys its own work on the Gmail thread
id and creates (or reuses) a separate WORK session with its own RunnerBinding
("created session 'c-alarm-…'"). The work session is archived when its task closes;
the anchor has no binding, so nothing ever reports it closed, and it stayed `active`
forever (25 open alarm anchors on 2026-10-29, back to 2026-09-16).

An anchor retires when BOTH hold:

* every turn on it is terminal (none queued / claimed / running / needs_human), and
* every work session those turns handed off to is archived (or no turn ever handed
  off — e.g. every turn was refused at the door).

Retirement is signal-driven (`close_turn`, a refused enqueue, and `sessions_closed`
for the work session) with a periodic sweep as the backstop that also backfills
anchors stranded before this existed (`sweep`, `manage.py retire_mail_anchors`).

New mail on a retired anchor revives it (`revive_for_mail`, under the same row lock
`maybe_retire` takes), so a retirement can never race a turn being enqueued.
"""
from __future__ import annotations

import logging
from datetime import timedelta

from django.db import transaction
from django.db.models import Q
from django.utils import timezone

from apps.canopy_sessions import lifecycle
from apps.canopy_sessions.models import RunnerBinding, Session

from .models import PendingMail, Turn
from .session_sync import EMAIL_THREAD_KEY, EMAIL_THREAD_PREFIX, fire_sessions_closed

logger = logging.getLogger(__name__)

#: A turnless anchor younger than this is left alone: `arrive` creates the session a
#: moment before `enqueue_turn` writes its turn.
TURNLESS_GRACE = timedelta(minutes=10)

RETIRED_REASON = "mail thread anchor: every turn finished and its work sessions closed"


def anchor_q() -> Q:
    """Sessions that are mail anchors: made for an email thread, never bound to a runner."""
    return (Q(metadata__source="email") & Q(**{f"metadata__{EMAIL_THREAD_KEY}__startswith":
                                                 EMAIL_THREAD_PREFIX})
            & Q(runner_binding__isnull=True))


def is_anchor(session) -> bool:
    meta = session.metadata or {}
    if meta.get("source") != "email" or not str(meta.get(EMAIL_THREAD_KEY) or "").startswith(
            EMAIL_THREAD_PREFIX):
        return False
    return not RunnerBinding.objects.filter(session_id=session.pk).exists()


def thread_id_of(session) -> str:
    key = str((session.metadata or {}).get(EMAIL_THREAD_KEY) or "")
    return key[len(EMAIL_THREAD_PREFIX):] if key.startswith(EMAIL_THREAD_PREFIX) else ""


def work_sessions(anchor) -> set:
    """Ids of the work sessions the anchor's turns handed off to.

    Joined two ways, because neither alone sees every turn: the turn's `session_key`
    on the runner that claimed it (`RunnerBinding.session_key`, the close-out join),
    and the runner's own thread key — the Gmail thread id, `#<capability>` for a
    confined caller (`session_naming.restricted_key`) — on the anchor's agent."""
    ids = set()
    keyed = (Turn.objects.filter(chat_session=anchor, claimed_by__isnull=False)
             .exclude(session_key="").values_list("session_key", "claimed_by_id"))
    for key, runner_id in keyed:
        ids.update(RunnerBinding.objects.filter(session_key=key, runner_id=runner_id)
                   .exclude(session_id=anchor.pk).values_list("session_id", flat=True))
    tid = thread_id_of(anchor)
    if tid and anchor.agent_id:
        ids.update(RunnerBinding.objects.filter(
            Q(thread_key=tid) | Q(thread_key__startswith=f"{tid}#"),
            session__agent_id=anchor.agent_id,
        ).exclude(session_id=anchor.pk).values_list("session_id", flat=True))
    return ids


def why_not(anchor, now=None) -> str:
    """Why `anchor` must stay open, or "" when it may retire."""
    now = now or timezone.now()
    if anchor.status == Session.ARCHIVED:
        return "already archived"
    turns = Turn.objects.filter(chat_session=anchor)
    live = turns.filter(status__in=list(Turn.NON_TERMINAL)).values_list("pk", flat=True).first()
    if live is not None:
        return f"turn {live} is not finished"
    if PendingMail.objects.filter(session=anchor).exists():
        return "mail on it is held for its quiet window (mail_coalesce)"
    if not turns.exists() and anchor.created_at and anchor.created_at > now - TURNLESS_GRACE:
        return "new, and its first turn may not be written yet"
    open_work = (Session.objects.filter(pk__in=work_sessions(anchor))
                 .exclude(status=Session.ARCHIVED).values_list("pk", flat=True).first())
    if open_work is not None:
        return f"its work session {open_work} is still open"
    return ""


def maybe_retire(session_id, now=None) -> bool:
    """Archive the anchor `session_id` if nothing on it is live. True if it retired.
    A no-op for any session that is not an anchor. Never raises."""
    try:
        with transaction.atomic():
            anchor = Session.objects.select_for_update().filter(pk=session_id).first()
            if anchor is None or not is_anchor(anchor) or why_not(anchor, now):
                return False
            anchor.status = Session.ARCHIVED
            with lifecycle.acting(reason=RETIRED_REASON, closing=True):
                anchor.save(update_fields=["status", "updated_at"])
            pk = anchor.pk
            transaction.on_commit(lambda: fire_sessions_closed([pk]))
        logger.info("mail anchor %s retired", session_id)
        return True
    except Exception:  # noqa: BLE001 — tidying a session is never worth a failed caller
        logger.exception("could not retire mail anchor %s", session_id)
        return False


def retire_later(session_id) -> None:
    """`maybe_retire` once the caller's transaction commits (now, outside one)."""
    if session_id:
        transaction.on_commit(lambda: maybe_retire(session_id))


def anchors_for_work_sessions(session_ids) -> set:
    """The anchors whose turns handed off to any of `session_ids`."""
    out = set()
    for b in RunnerBinding.objects.filter(session_id__in=list(session_ids)).select_related("session"):
        if b.session_key and b.runner_id:
            out.update(Turn.objects.filter(session_key=b.session_key, claimed_by_id=b.runner_id,
                                           chat_session__isnull=False)
                       .values_list("chat_session_id", flat=True))
        tid = (b.thread_key or "").split("#", 1)[0]
        if tid and b.session.agent_id:
            out.update(Session.objects.filter(
                agent_id=b.session.agent_id,
                **{f"metadata__{EMAIL_THREAD_KEY}": f"{EMAIL_THREAD_PREFIX}{tid}"},
            ).values_list("pk", flat=True))
    return out - set(session_ids)


def on_sessions_closed(session_ids) -> int:
    """A work session closed: retire the anchors it was the last live thing on."""
    try:
        anchors = anchors_for_work_sessions(session_ids)
    except Exception:  # noqa: BLE001
        logger.exception("could not find mail anchors for closed sessions")
        return 0
    return sum(1 for pk in anchors if maybe_retire(pk))


def revive_for_mail(session) -> None:
    """New mail on a thread: its anchor is live again. Call inside the transaction
    that writes the turn — the row lock is what keeps `maybe_retire` from archiving
    the anchor between the check and the turn's insert."""
    row = Session.objects.select_for_update().filter(pk=session.pk).first()
    if row is None or row.status != Session.ARCHIVED or not is_anchor(row):
        return
    row.status = Session.ACTIVE
    with lifecycle.acting(reason="new mail on the thread"):
        row.save(update_fields=["status", "updated_at"])
    session.status = Session.ACTIVE


def candidates():
    return Session.objects.filter(anchor_q()).exclude(status=Session.ARCHIVED)


def sweep(now=None, *, dry_run: bool = False) -> list:
    """Retire every open anchor nothing is live on (backstop + backfill). Returns
    the ids retired (or, with `dry_run`, that would be)."""
    now = now or timezone.now()
    done = []
    for pk in list(candidates().values_list("pk", flat=True)):
        if dry_run:
            s = Session.objects.filter(pk=pk).first()
            if s is not None and not why_not(s, now):
                done.append(pk)
        elif maybe_retire(pk, now):
            done.append(pk)
    return done

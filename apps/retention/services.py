"""The purge: find content past its rule's `keep_days` and drop it.

`sweep(apply=False)` is a dry run that only counts. `sweep(apply=True)`
deletes. `maybe_sweep()` is the heartbeat's door, and does nothing unless
`CANOPY_RETENTION_ENFORCE` is on. What each kind drops, and what it keeps, is
in the spec (docs/superpowers/specs/2026-10-05-content-retention-design.md);
the short version:

- a turn keeps its row and loses prompt, result note, report summary, ledger,
  raw transcript and its Slack status text;
- a chat loses a PREFIX of its messages (everything up to the newest expired
  one), its old drafts, attachments and page actions, and its session
  `retention_floor_index` rises so a runner re-ship cannot write them back.
  Once the whole session is idle past the cutoff it also loses its title and
  live-state caches and is archived;
- a shared transcript is deleted.
"""
from __future__ import annotations

import datetime as dt
import logging
from collections import defaultdict

from django.conf import settings
from django.core.cache import cache
from django.db import transaction
from django.db.models import Max, Q
from django.db.models.functions import Coalesce
from django.utils import timezone

from .models import RetentionRule, RetentionSweep
from .policy import Policy, Subject, chat_principal, chat_source, turn_principal

logger = logging.getLogger(__name__)

#: Most work one run does, so one heartbeat never carries a long purge. A
#: backlog drains over successive runs; `retention_sweep --apply --no-limit`
#: clears it in one go.
BATCH_LIMITS = {"turns": 5000, "chats": 100, "shared": 500}
#: How often the heartbeat may start a sweep.
SWEEP_INTERVAL = dt.timedelta(hours=1)
_LOCK_KEY = "retention:sweep"


class Tally:
    """Counts per rule. Keyed by rule id; `as_json` labels them for a person."""

    def __init__(self):
        self.by_rule: dict[int, dict[str, int]] = defaultdict(lambda: defaultdict(int))
        self.rules: dict[int, RetentionRule] = {}

    def add(self, rule: RetentionRule, what: str, n: int = 1) -> None:
        if n:
            self.rules[rule.pk] = rule
            self.by_rule[rule.pk][what] += n

    def totals(self) -> dict[str, int]:
        out: dict[str, int] = defaultdict(int)
        for counts in self.by_rule.values():
            for what, n in counts.items():
                out[what] += n
        return dict(out)

    def as_json(self) -> dict:
        return {
            "totals": self.totals(),
            "by_rule": {_label(self.rules[pk]): dict(c) for pk, c in sorted(self.by_rule.items())},
        }


def _label(rule: RetentionRule) -> str:
    return f"#{rule.pk} {rule.scope_label()} {rule.filter_label()} keep {rule.keep_days}d"


def _cutoff(rule: RetentionRule | None, now: dt.datetime) -> dt.datetime | None:
    if rule is None or rule.keep_days is None:
        return None
    return now - dt.timedelta(days=rule.keep_days)


# ---------------------------------------------------------------- entry points

def sweep(*, apply: bool, now: dt.datetime | None = None, trigger: str = "command",
          limits: dict[str, int | None] | None = None) -> RetentionSweep:
    """One pass over every kind. Returns the recorded `RetentionSweep`."""
    now = now or timezone.now()
    limits = {**BATCH_LIMITS, **(limits or {})}
    record = RetentionSweep.objects.create(applied=apply, trigger=trigger)
    tally = Tally()
    try:
        _run(tally, apply=apply, now=now, limits=limits)
    except Exception as exc:  # noqa: BLE001 - recorded, then re-raised
        record.error = f"{type(exc).__name__}: {exc}"[:2000]
        raise
    finally:
        record.counts = tally.as_json()
        record.finished_at = timezone.now()
        record.save(update_fields=["counts", "finished_at", "error"])
    return record


def preview(workspace: str, *, now: dt.datetime | None = None) -> Tally:
    """What the CURRENT rules would drop from this one workspace's own chats and
    turns, were they enforced now. Never writes, never recorded, never capped:
    it is the answer to "what will this rule do", which a capped count would
    misstate. Descendant workspaces are not included; their admins preview their
    own."""
    tally = Tally()
    _run(tally, apply=False, now=now or timezone.now(),
         limits={k: None for k in BATCH_LIMITS}, workspace=workspace)
    return tally


def _run(tally: Tally, *, apply: bool, now, limits, workspace: str | None = None) -> None:
    policy = Policy.load()
    shortest = policy.shortest_keep_days
    if shortest is None:
        return
    horizon = now - dt.timedelta(days=shortest)
    common = dict(now=now, horizon=horizon, apply=apply, workspace=workspace)
    _sweep_turns(policy, tally, limit=limits["turns"], **common)
    _sweep_chats(policy, tally, limit=limits["chats"], **common)
    if workspace is None:  # shared transcripts belong to no workspace
        _sweep_shared(policy, tally, limit=limits["shared"], now=now, horizon=horizon, apply=apply)


def maybe_sweep(now: dt.datetime | None = None) -> RetentionSweep | None:
    """The heartbeat's door. Off unless `CANOPY_RETENTION_ENFORCE`; at most one
    run per `SWEEP_INTERVAL` across the fleet (the cache is Redis in labs).
    Never raises: a purge must not cost a runner its heartbeat."""
    if not getattr(settings, "CANOPY_RETENTION_ENFORCE", False):
        return None
    if not cache.add(_LOCK_KEY, "1", timeout=int(SWEEP_INTERVAL.total_seconds())):
        return None
    try:
        return sweep(apply=True, now=now, trigger="heartbeat")
    except Exception:  # noqa: BLE001
        logger.exception("retention: sweep failed")
        return None


# ---------------------------------------------------------------------- turns

def _expired_turns(qs):
    from apps.harness.models import Turn

    return (
        qs.filter(status__in=Turn.TERMINAL, content_purged_at__isnull=True)
        .annotate(aged_at=Coalesce("finished_at", "created_at"))
    )


def _sweep_turns(policy, tally, *, now, horizon, apply, limit, workspace=None) -> None:
    """Agent and project turns. A turn on a chat is the chat's, see _sweep_chats."""
    from apps.harness.models import Turn

    qs = Turn.objects.filter(chat_session__isnull=True)
    if workspace is not None:
        qs = qs.filter(Q(agent__workspace_id=workspace) | Q(agent__isnull=True, workspace_id=workspace))
    rows = (
        _expired_turns(qs)
        .filter(aged_at__lt=horizon)
        .values_list("pk", "origin", "initiator_kind", "agent_id",
                     "agent__workspace_id", "workspace_id", "aged_at")
        .order_by("aged_at")
    )
    picked: list[tuple] = []
    for pk, origin, initiator_kind, agent_id, agent_ws, project_ws, aged_at in rows.iterator():
        rule = policy.resolve(Subject(
            kind=RetentionRule.TURN, workspace=agent_ws or project_ws, source=origin,
            principal=turn_principal(initiator_kind), agent_id=agent_id,
        ))
        cutoff = _cutoff(rule, now)
        if cutoff is None or aged_at >= cutoff:
            continue
        picked.append((pk, rule))
        if limit is not None and len(picked) >= limit:
            break
    for pk, rule in picked:
        tally.add(rule, "turns_scrubbed")
    if apply and picked:
        _scrub_turns([pk for pk, _ in picked], now)


def _scrub_turns(turn_ids: list, now: dt.datetime) -> None:
    from apps.harness.models import CallerToken, Turn, TurnEvent, TurnTranscript
    from apps.slack.models import SlackTurnPost

    for start in range(0, len(turn_ids), 500):
        ids = turn_ids[start:start + 500]
        with transaction.atomic():
            Turn.objects.filter(pk__in=ids).update(
                prompt="", result_note="", report_summary="", content_purged_at=now,
            )
            TurnEvent.objects.filter(turn_id__in=ids).delete()
            TurnTranscript.objects.filter(turn_id__in=ids).delete()
            CallerToken.objects.filter(turn_id__in=ids).delete()
            # The status line carries the whole ask (slack #1134).
            SlackTurnPost.objects.filter(turn_id__in=ids).update(rendered="", prefix="")


# ---------------------------------------------------------------------- chats

def _sweep_chats(policy, tally, *, now, horizon, apply, limit, workspace=None) -> None:
    """Chat sessions, and every turn on them, resolved as ONE unit by the
    session's attributes so a conversation's messages and the prompts that
    produced them expire together."""
    from apps.canopy_sessions.models import Session

    sessions = Session.objects.all() if workspace is None else Session.objects.filter(workspace_id=workspace)
    rows = (
        sessions.filter(pk__in=_chat_candidates(horizon))
        .values_list("pk", "workspace_id", "agent_id", "contact_id", "created_by_id",
                     "origin", "metadata")
        .order_by("created_at")
    )
    touched = 0
    for pk, workspace_id, agent_id, contact_id, created_by_id, origin, metadata in rows.iterator():
        rule = policy.resolve(Subject(
            kind=RetentionRule.CHAT, workspace=workspace_id,
            source=chat_source(metadata=metadata, origin=origin),
            principal=chat_principal(contact_id=contact_id, created_by_id=created_by_id),
            agent_id=agent_id,
        ))
        cutoff = _cutoff(rule, now)
        if cutoff is None:
            continue
        if _purge_chat(pk, rule, cutoff, tally, now=now, apply=apply):
            touched += 1
            if limit is not None and touched >= limit:
                return


def _chat_candidates(horizon) -> set:
    """Sessions holding ANYTHING older than the shortest rule, in a handful of
    bulk queries, so the per-session work (~10 queries) runs only where there
    is something to do rather than on every old session every hour. Every
    rule's cutoff is at or before `horizon`, so this is a superset of what any
    rule can expire."""
    from apps.canopy_sessions.models import (
        Attachment,
        Draft,
        Message,
        PageAction,
        Session,
        TransferRequest,
    )
    from apps.harness.models import Turn

    def ids(qs):
        return set(qs.values_list("session_id", flat=True).distinct())

    out = ids(Message.objects.filter(created_at__lt=horizon))
    out |= ids(Attachment.objects.filter(created_at__lt=horizon))
    out |= ids(Draft.objects.filter(updated_at__lt=horizon).exclude(body=""))
    out |= ids(PageAction.objects.filter(created_at__lt=horizon))
    out |= ids(TransferRequest.objects.filter(created_at__lt=horizon).exclude(brief=""))
    out |= set(
        _expired_turns(Turn.objects.filter(chat_session__isnull=False))
        .filter(aged_at__lt=horizon).values_list("chat_session_id", flat=True).distinct()
    )
    # Not yet emptied and archived: a candidate for the idle close.
    out |= set(
        Session.objects.filter(updated_at__lt=horizon)
        .exclude(title="", status=Session.ARCHIVED, page_state={})
        .values_list("pk", flat=True)
    )
    return out


def _purge_chat(session_id, rule, cutoff, tally, *, now, apply) -> bool:
    """Count (and, applying, drop) one session's expired content. True if it
    had any."""
    from apps.canopy_sessions.models import (
        Attachment,
        Draft,
        Message,
        PageAction,
        RunnerBinding,
        Session,
        TransferRequest,
    )
    from apps.harness.models import Turn

    newest_expired = (
        Message.objects.filter(session_id=session_id, created_at__lt=cutoff)
        .aggregate(m=Max("turn_index"))["m"]
    )
    messages = (
        Message.objects.filter(session_id=session_id, turn_index__lte=newest_expired)
        if newest_expired is not None else Message.objects.none()
    )
    attachments = Attachment.objects.filter(session_id=session_id, created_at__lt=cutoff)
    drafts = Draft.objects.filter(session_id=session_id, updated_at__lt=cutoff).exclude(body="")
    actions = PageAction.objects.filter(session_id=session_id, created_at__lt=cutoff)
    briefs = TransferRequest.objects.filter(
        session_id=session_id, created_at__lt=cutoff,
    ).exclude(brief="")
    turn_ids = list(
        _expired_turns(Turn.objects.filter(chat_session_id=session_id))
        .filter(aged_at__lt=cutoff).values_list("pk", flat=True)
    )
    session = Session.objects.get(pk=session_id)
    binding = RunnerBinding.objects.filter(session_id=session_id).first()
    idle = session.updated_at < cutoff and not (
        binding and any(t and t >= cutoff for t in (binding.last_interacted_at, binding.live_seen_at))
    ) and not Message.objects.filter(session_id=session_id, created_at__gte=cutoff).exists()
    closes = idle and (
        bool(session.title) or session.status == Session.ACTIVE or bool(session.page_state)
        or bool(binding and (binding.tail or binding.summary or binding.pending_question))
    )

    counts = {
        "chat_messages": messages.count(),
        "chat_attachments": attachments.count(),
        "chat_drafts": drafts.count(),
        "chat_page_actions": actions.count(),
        "chat_transfer_briefs": briefs.count(),
        "chat_turns_scrubbed": len(turn_ids),
        "chats_closed": int(closes),
    }
    if not any(counts.values()):
        return False
    for what, n in counts.items():
        tally.add(rule, what, n)
    tally.add(rule, "chats_touched")
    if not apply:
        return True

    attachments = attachments.exclude(pk__in=_delete_attachment_bytes(attachments))
    with transaction.atomic():
        locked = Session.objects.select_for_update().get(pk=session_id)
        # .update(), not save(): auto_now would bump updated_at, and a purge is
        # not activity. The floor only rises.
        fields = {"content_purged_at": now}
        if newest_expired is not None:
            fields["retention_floor_index"] = max(locked.retention_floor_index, newest_expired + 1)
        if closes:
            fields.update(title="", page_state={}, page_actions_available=[],
                          status=Session.ARCHIVED, finish_push_due_at=None)
        Session.objects.filter(pk=session_id).update(**fields)
        messages.delete()
        attachments.delete()
        drafts.delete()
        actions.delete()
        briefs.update(brief="")
        if closes and binding is not None:
            RunnerBinding.objects.filter(pk=binding.pk).update(
                tail=[], summary="", pending_question=None, pending_answer=None,
            )
    if turn_ids:
        _scrub_turns(turn_ids, now)
    return True


def _delete_attachment_bytes(attachments) -> set:
    """Bytes before rows, best-effort, as in `sweep_unbound_attachments`.
    Returns the ids whose bytes could NOT be deleted, so the caller keeps their
    rows: a row that outlives a failed byte-delete is retried by the next
    sweep, while a blob that outlives its row is orphaned for good."""
    from apps.canopy_sessions import attachment_storage

    if not attachment_storage.is_configured():
        return set()
    failed = set()
    for pk, key in attachments.values_list("pk", "storage_key"):
        try:
            attachment_storage.delete(key)
        except Exception:  # noqa: BLE001
            logger.warning("retention: could not delete bytes for attachment %s", pk, exc_info=True)
            failed.add(pk)
    return failed


# --------------------------------------------------------- shared transcripts

def _sweep_shared(policy, tally, *, now, horizon, apply, limit) -> None:
    """Uploaded `/canopy:share-session` transcripts carry no workspace, so only
    deployment-wide rules reach them."""
    from apps.session_sharing.models import Session as SharedSession

    rule = policy.resolve(Subject(kind=RetentionRule.SHARED, workspace=None,
                                  principal=RetentionRule.MEMBER))
    cutoff = _cutoff(rule, now)
    if cutoff is None:
        return
    ids = SharedSession.objects.filter(created_at__lt=cutoff).order_by("created_at").values_list("pk", flat=True)
    ids = list(ids[:limit] if limit is not None else ids)
    tally.add(rule, "shared_transcripts_deleted", len(ids))
    if apply and ids:
        SharedSession.objects.filter(pk__in=ids).delete()

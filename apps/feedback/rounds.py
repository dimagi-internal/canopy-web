"""Feedback-round lifecycle (framework). Request-free and target-agnostic: the
product app that owns the target decides who may open/close and what closing
does; this module only keeps the state machine honest."""
from __future__ import annotations

import uuid

from django.db import IntegrityError, transaction
from django.db.models import Max
from django.utils import timezone

from apps.feedback.models import Feedback, FeedbackRound


class RoundError(Exception):
    def __init__(self, status: int, detail: str):
        super().__init__(detail)
        self.status = status
        self.detail = detail


def _of(workspace_id, target_kind, target_ref):
    return FeedbackRound.objects.filter(workspace_id=workspace_id, target_kind=target_kind,
                                        target_ref=target_ref)


def current(*, workspace_id, target_kind, target_ref):
    return _of(workspace_id, target_kind, target_ref).filter(state=FeedbackRound.OPEN).first()


def by_number(*, workspace_id, target_kind, target_ref, number):
    return _of(workspace_id, target_kind, target_ref).filter(number=number).first()


def latest_finished(*, workspace_id, target_kind, target_ref):
    return (_of(workspace_id, target_kind, target_ref)
            .exclude(state=FeedbackRound.OPEN).order_by("-number").first())


@transaction.atomic
def open_round(*, workspace, target_kind, target_ref, pinned_versions, by_user=None, adopt=None):
    existing = current(workspace_id=workspace.pk, target_kind=target_kind, target_ref=target_ref)
    if existing is not None:
        raise RoundError(409, f"round {existing.number} is already open")
    last = _of(workspace.pk, target_kind, target_ref).aggregate(m=Max("number"))["m"] or 0
    try:
        with transaction.atomic():
            rnd = FeedbackRound.objects.create(
                workspace=workspace, target_kind=target_kind, target_ref=target_ref,
                number=last + 1, pinned_versions=dict(pinned_versions or {}),
                opened_by=by_user if getattr(by_user, "is_authenticated", False) else None,
            )
    except IntegrityError:
        raise RoundError(409, "a round is already open") from None
    if adopt is not None:
        Feedback.objects.filter(pk__in=adopt.filter(round__isnull=True).values("pk")).update(round=rnd)
    return rnd


def lock_open(rnd) -> FeedbackRound:
    """Re-read under a row lock; refuse unless still open. Call inside atomic()."""
    rnd = FeedbackRound.objects.select_for_update().get(pk=rnd.pk)
    if rnd.state != FeedbackRound.OPEN:
        raise RoundError(409, f"round {rnd.number} is {rnd.state}")
    return rnd


def ingest_into(rnd, items, *, workspace, submitted_by=None) -> dict:
    """Ingest `items` into `rnd` only if it is STILL open, else into no round.

    The caller read the round outside any lock; a close can land in between. Re-read
    it under a row lock so a note never attaches to a closed round: it stays loose
    and the next round adopts it when consume opens it."""
    from apps.feedback import services

    with transaction.atomic():
        live = None
        if rnd is not None:
            locked = FeedbackRound.objects.select_for_update().get(pk=rnd.pk)
            live = locked if locked.state == FeedbackRound.OPEN else None
        return services.ingest(items, workspace=workspace, submitted_by=submitted_by, round=live)


def stage_edit(rnd, edit: dict, *, max_edits: int | None = None) -> dict:
    """Append one staged edit under the round's lock. `max_edits` caps the list —
    checked under the lock, so concurrent stagers cannot overshoot it."""
    with transaction.atomic():
        rnd = lock_open(rnd)
        if max_edits is not None and len(rnd.staged_edits) >= max_edits:
            raise RoundError(409, f"round {rnd.number} has too many staged edits "
                                  f"({max_edits}); close it and continue in the next one")
        entry = {**edit, "edit_id": uuid.uuid4().hex[:12], "at": timezone.now().isoformat()}
        rnd.staged_edits = [*rnd.staged_edits, entry]
        rnd.save(update_fields=["staged_edits"])
    return entry


def mark_closed(rnd, *, by_user, note: str) -> None:
    rnd.state = FeedbackRound.CLOSED
    rnd.closed_by = by_user if getattr(by_user, "is_authenticated", False) else None
    rnd.closed_at = timezone.now()
    rnd.close_note = (note or "").strip()
    rnd.save(update_fields=["state", "closed_by", "closed_at", "close_note"])


def unanswered(rnd) -> list[int]:
    return sorted(rnd.notes.filter(state=Feedback.STATE_NEW).values_list("pk", flat=True))


@transaction.atomic
def mark_consumed(rnd, *, run_id: str) -> FeedbackRound:
    rnd = FeedbackRound.objects.select_for_update().get(pk=rnd.pk)
    if rnd.state != FeedbackRound.CLOSED:
        raise RoundError(409, f"round {rnd.number} is {rnd.state}; only a closed round is consumed")
    left = unanswered(rnd)
    if left:
        raise RoundError(409, "every note needs an answer before the round is consumed; "
                              f"still new: {', '.join(map(str, left))}")
    rnd.state = FeedbackRound.CONSUMED
    rnd.consumed_by_run = (run_id or "").strip()
    rnd.consumed_at = timezone.now()
    rnd.save(update_fields=["state", "consumed_by_run", "consumed_at"])
    return rnd

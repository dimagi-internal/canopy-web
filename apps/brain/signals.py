"""An activity fold marks its source due for association (`associate.schedule`), after the commit.

Observed on the `Session` save itself, so no ingest path has to remember: `activity.store` saves
`update_fields=["activity", "activity_keys"]` and that is the fold."""
from __future__ import annotations

from django.db import transaction
from django.db.models.signals import post_save
from django.dispatch import receiver

from apps.harness.signals import session_content_deleted, turn_status_changed

from . import associate, cascade, runs
from .models import BrainRun, BrainSource


@receiver(post_save, sender="canopy_sessions.Session", dispatch_uid="brain_activity_fold_schedules_association")
def _schedule_after_fold(sender, instance, created, update_fields=None, **kwargs):
    if update_fields is None or "activity" not in update_fields:
        return
    session_id = instance.pk
    transaction.on_commit(lambda: associate.schedule(session_id))


@receiver(session_content_deleted, dispatch_uid="brain_content_deleted_cascades")
def _cascade_content_deletion(sender, session, **kwargs):
    """The person deleted a session's words: its source is withdrawn for good and what came from it goes
    (`cascade`), once the deleting transaction commits."""
    ids = list(BrainSource.objects.filter(kind=BrainSource.SESSION, ref=str(session.pk)).values_list("pk", flat=True))
    if ids:
        cascade.after_commit(ids)


@receiver(turn_status_changed, dispatch_uid="brain_run_ends_with_its_turn")
def _run_ends_with_its_turn(sender, turn, **kwargs):
    """A turn in a brain run's conversation reached an end: the run is over, and a clean end moves the
    feature's cursor (`runs.finish`)."""
    from apps.harness.models import Turn  # noqa: PLC0415 — receivers load at ready()

    if turn.status not in Turn.TERMINAL or not turn.chat_session_id:
        return
    run = BrainRun.objects.filter(session_id=turn.chat_session_id, state=BrainRun.OPEN).select_related("capability").first()
    if run is not None:
        runs.finish(run, ok=turn.status == Turn.DONE)

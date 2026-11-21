"""An activity fold marks its source due for association (`associate.schedule`), after the commit.

Observed on the `Session` save itself, so no ingest path has to remember: `activity.store` saves
`update_fields=["activity", "activity_keys"]` and that is the fold.

Who is in a source follows the session (`sources.refresh_session`), after the commit: a
`SessionParticipant` added or taken out, and a line or a turn by someone not yet in the source.
Cheap: a line or turn by someone already in it costs one lookup. A line stored in bulk sends no
signal; `eligibility` reads the session live as well, and the associator refreshes on its next look."""
from __future__ import annotations

from django.db import transaction
from django.db.models.signals import post_delete, post_save
from django.dispatch import receiver

from apps.harness.signals import session_content_deleted, turn_status_changed

from . import associate, cascade, runs, sources
from .models import BrainRun, BrainSource


@receiver(post_save, sender="canopy_sessions.Session", dispatch_uid="brain_activity_fold_schedules_association")
def _schedule_after_fold(sender, instance, created, update_fields=None, **kwargs):
    if update_fields is None or "activity" not in update_fields:
        return
    session_id = instance.pk
    transaction.on_commit(lambda: associate.schedule(session_id))


@receiver(post_save, sender="canopy_sessions.Session", dispatch_uid="brain_a_moved_session_follows")
def _a_moved_session(sender, instance, created, update_fields=None, **kwargs):
    """A session moved to another workspace: its source follows it after the commit (`sources.refresh`), and its
    links to projects of the workspace it left go (`sources.drop_foreign_links`)."""
    if created or (update_fields is not None and "workspace" not in update_fields):
        return
    if (BrainSource.objects.filter(kind=BrainSource.SESSION, ref=str(instance.pk))
            .exclude(workspace_id=instance.workspace_id).exists()):
        _refresh_after_commit(instance.pk)


#: The `Turn` fields that say who started it and in which conversation.
_WHO_AND_WHERE = frozenset({"initiator_user", "initiator_contact", "chat_session", "canopy_session"})


def _refresh_after_commit(session_id) -> None:
    if session_id is not None:
        transaction.on_commit(lambda: sources.refresh_session(session_id))


@receiver(post_save, sender="canopy_sessions.SessionParticipant", dispatch_uid="brain_participant_added")
@receiver(post_delete, sender="canopy_sessions.SessionParticipant", dispatch_uid="brain_participant_removed")
def _participants_follow_the_session(sender, instance, **kwargs):
    _refresh_after_commit(instance.session_id)


@receiver(post_save, sender="canopy_sessions.Message", dispatch_uid="brain_a_new_voice_in_a_session")
def _a_line_by_someone_new(sender, instance, created, **kwargs):
    author = instance.author if isinstance(instance.author, dict) else None
    if not created or not author:
        return
    uid, cid = author.get("user_id"), author.get("contact_id")
    if uid is None and cid is None:
        return
    if not sources.is_participant(instance.session_id, user_id=uid, contact_id=None if uid is not None else cid):
        _refresh_after_commit(instance.session_id)


@receiver(post_save, sender="harness.Turn", dispatch_uid="brain_a_turn_by_someone_new")
def _a_turn_by_someone_new(sender, instance, created, update_fields=None, **kwargs):
    if not created and update_fields is not None and not _WHO_AND_WHERE & set(update_fields):
        return      # a status change: who started it and where did not move
    uid, cid = instance.initiator_user_id, instance.initiator_contact_id
    if uid is None and cid is None:
        return
    for session_id in {instance.chat_session_id, instance.canopy_session_id} - {None}:
        if not sources.is_participant(session_id, user_id=uid, contact_id=None if uid is not None else cid):
            _refresh_after_commit(session_id)


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

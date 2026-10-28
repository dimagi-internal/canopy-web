"""Project the harness ledger into chat Messages.

Subscribes to harness's turn_events_appended (fired post-commit, after bulk_create,
which emits no post_save) and materializes a session turn's assistant/tool events
into Message rows. Same signal SP1's realtime fan-out rides — chat is just a second,
independent consumer of it.
"""
from __future__ import annotations

from django.db.models.signals import post_delete, post_save
from django.dispatch import receiver

from apps.harness import sensitivity
from apps.harness.signals import (
    sessions_reported,
    transcript_rows_streamed,
    turn_events_appended,
    turn_status_changed,
)

from . import conversation_keys


@receiver(post_save, sender="canopy_sessions.Session", dispatch_uid="chat_sensitive_session_key")
def _key_a_sensitive_session_at_birth(sender, instance, created, **kwargs):
    """A conversation born sensitive (a sensitive site's token, a sensitive parent)
    gets its key before its first row (`conversation_keys.ensure_key`): the one its
    starter's browser made when one did (kept even for a conversation not sensitive
    yet, so a later turn that makes it so seals to it), else one made here."""
    if not created:
        return
    offered = getattr(instance, "_offered_key", None)
    if offered is not None or sensitivity.is_sensitive(instance):
        conversation_keys.ensure_key(instance, offered=offered)


@receiver(post_delete, sender="canopy_sessions.Attachment", dispatch_uid="chat_private_attachment_objects")
def _private_attachment_objects_go_with_it(sender, instance, **kwargs):
    """A private conversation's file row going, however it goes (a deleted session's
    cascade among them), takes both its objects with it, on commit
    (`private_attachments.delete_all`; deleting one already gone is a no-op)."""
    if instance.sensitive:
        from . import private_attachments  # noqa: PLC0415 — models: imported at app ready

        private_attachments.delete_all([instance])


@receiver(turn_events_appended, dispatch_uid="chat_project_messages")
def _project_messages(sender, turn, rows, **kwargs):
    if not turn.chat_session_id:
        return
    from .transcript_ingest import project_events

    project_events(turn, rows)
    # The turn moved. Same signal, same reason as the Slack status line: a
    # `status` row IS the transition, and re-deriving is cheap next to the
    # append that just happened.
    #
    # The session's LATEST turn, not necessarily this one: when the previous
    # message ends with a follow-up queued behind it, the follow-up is what the
    # page is waiting on, and pushing the finished turn's "done" would drop the
    # bubble over a reply that is still coming (#1147).
    if any(r.kind == "status" for r in rows):
        from .status_feed import publish_for_session

        publish_for_session(turn.chat_session_id)


@receiver(turn_status_changed, dispatch_uid="chat_turn_status_enqueued")
def _status_on_enqueue(sender, turn, **kwargs):
    """The moment the ask exists. The one a person is actually looking at.

    Before this the first thing a chat page heard about a send was the reply —
    so a turn queued behind a closed laptop rendered as an empty panel with no
    way to tell it apart from one being worked on.
    """
    from .status_feed import publish_for_turn

    publish_for_turn(turn)


@receiver(transcript_rows_streamed, dispatch_uid="chat_queued_on_transcript")
def _queued_on_transcript(sender, session, rows, **kwargs):
    """A user row landed in the transcript, so the send it carries drops off the
    queued list — re-derive and push it rather than waiting for the next status
    transition, which may be a while behind a streamed reply."""
    if not any(kind == "user" for _index, kind, _text in rows):
        return
    from .queued_feed import publish_queued

    try:
        publish_queued(session.pk)
    except Exception:  # noqa: BLE001 — never break a runner's stream over the list
        import logging

        logging.getLogger(__name__).exception("queued-list push failed on transcript stream")


@receiver(sessions_reported, dispatch_uid="chat_turn_status_sweep")
def _status_sweep(sender, runner, **kwargs):
    """A runner died and cannot say so. Found on other runners' reports."""
    from .status_feed import sweep

    try:
        sweep()
    except Exception:  # noqa: BLE001 — never break a runner's report over a status
        import logging

        logging.getLogger(__name__).exception("turn-status sweep failed")


# --- lifecycle events ----------------------------------------------------------
#
# Every session transition a save makes — created, closed/archived/unarchived,
# moved, Slack bound/detached, sensitivity — and every participant row added,
# re-roled or removed is one `session.*` event in the workspace's log
# (`lifecycle.py`). Observed on the rows, so no save path has to remember.

from django.db.models.signals import post_init  # noqa: E402

from . import lifecycle  # noqa: E402


@receiver(post_init, sender="canopy_sessions.Session", dispatch_uid="session_lifecycle_snapshot")
def _session_lifecycle_snapshot(sender, instance, **kwargs):
    instance._lifecycle_snapshot = lifecycle.snapshot(instance)


@receiver(post_save, sender="canopy_sessions.Session", dispatch_uid="session_lifecycle_saved")
def _session_lifecycle_saved(sender, instance, created=False, raw=False, update_fields=None, **kwargs):
    if raw:
        return
    lifecycle.observe_saved(instance, created=created, update_fields=update_fields)


@receiver(post_delete, sender="canopy_sessions.Session", dispatch_uid="session_lifecycle_deleted")
def _session_lifecycle_deleted(sender, instance, origin=None, **kwargs):
    lifecycle.observe_deleted(instance)


@receiver(post_init, sender="canopy_sessions.SessionParticipant", dispatch_uid="participant_lifecycle_snapshot")
def _participant_lifecycle_snapshot(sender, instance, **kwargs):
    instance._lifecycle_role = lifecycle.participant_snapshot(instance)


@receiver(post_save, sender="canopy_sessions.SessionParticipant", dispatch_uid="participant_lifecycle_saved")
def _participant_lifecycle_saved(sender, instance, created=False, raw=False, update_fields=None, **kwargs):
    if raw:
        return
    lifecycle.observe_participant(instance, created=created, update_fields=update_fields)


@receiver(post_delete, sender="canopy_sessions.SessionParticipant", dispatch_uid="participant_lifecycle_deleted")
def _participant_lifecycle_deleted(sender, instance, origin=None, **kwargs):
    lifecycle.observe_participant(instance, deleted=True, origin=origin)

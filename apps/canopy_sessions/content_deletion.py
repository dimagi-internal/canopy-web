"""A person permanently deleting the stored words of their own session.

Who may is `access.owns_words`, asked at the route with `harness.acting_person`
(the person themselves, never an agent, a turn's token or a connected site). This
module says when (`blocker`) and what happens (`delete_content`).

What goes: every place that holds what was said. `tests/test_session_content_deletion.py`
walks every model pointing at a `Session` or a `Turn` and fails on one nobody has placed
as words, ids or someone else's record, so a new table cannot be forgotten here.

What stays: the `Session` and `Turn` rows and their ids, the binding's identity
(`session_key`, `transcript_id`), `SessionUsage` and `Activity`. Analytics reads only
those last two (`docs/architecture/analytics.md`), and matches a box's usage report to
canopy's record of the Claude session by those ids, so it does not change.

Why a tombstone. The rows are a cache of a file on the runner's disk, and that file is not
canopy's to delete: left alone, the next stream, backfill, reset or coaching upload would
send everything straight back. So `Session.content_deleted_at` is checked by every path
that takes words from a runner (`transcript_ingest.refuses_ship`, the funnel), the paths
that ask a runner for them (`list_streams`, `list_backfills`, `request_backfill`, `reset`,
`coaching.full_upload_session_ids`), the report's caches (`session_sync`) and every send
(`send._send_message`, `enqueue_turn`, `access.can_write`). `DeletedTranscript` holds the
Claude transcript ids, so the same file shipped into a new session row (a box paired again)
is dropped as well.

What it cannot reach, and the person is told: the transcript on their own computer, anything
already read out of canopy (a coach's notes, its turn logs and its mailbox), words posted
to Slack, a `/canopy:share-session` link they made (its own delete), facts on their memory
page, and work this conversation dispatched to other sessions, which keep their own records. That
includes the chat a `SessionReviewAsk` opened with a coach: it may quote this one, and it
is a conversation of its own that its person can delete the same way.
"""
from __future__ import annotations

import logging

from django.db import transaction
from django.db.models import Max
from django.utils import timezone

from apps.admission.models import AdmissionNotice
from apps.agents.models import AgentSecretRequest
from apps.events.models import SessionUsage
from apps.harness import runner_requirements as rr
from apps.harness import sensitivity
from apps.harness.models import (
    ActAsGrant,
    CallerToken,
    FailureInvestigation,
    PendingMail,
    Turn,
    TurnEvent,
    TurnTranscript,
)
from apps.harness.signals import session_content_deleted
from apps.realtime import groups

from . import attachment_storage, lifecycle, session_state, transcript_ingest
from . import services as chat_services
from .models import (
    AppViewSubscription,
    Attachment,
    ChatKey,
    DeletedTranscript,
    Draft,
    HandoffArchive,
    Message,
    PageAction,
    RunnerBinding,
    Session,
    SessionHandoff,
    SessionKey,
    SessionKeyWrap,
    SessionReviewAsk,
    SessionSecret,
    TransferRequest,
)
from .placement import FORK_KEY

logger = logging.getLogger(__name__)

#: `blocker`'s answers. Stable strings: the refusal's message is `REFUSALS[it]`.
RUNNING, MOVING, HAS_FORK = "running", "moving", "has_fork"
REFUSALS = {
    RUNNING: "This session is still working. Stop it or wait for it to finish, then delete it.",
    MOVING: "This session is being moved to another runner. Delete it once the move has finished.",
    HAS_FORK: "A fork of this session holds a copy of these words. Delete the fork first; "
              "if it is someone else's, ask them.",
}

#: What a session's `metadata` keeps: what routes and reads it, never what it said. An
#: allow-list, so a key added later is dropped until someone says it holds no words.
_METADATA_KEPT = frozenset({
    rr.METADATA_KEY, chat_services.TRANSCRIPT_SOURCED, chat_services.EMBED_APP_KEY, FORK_KEY,
    transcript_ingest.TASK_UID_KEY, "source", "origin_key", "opp_slug", "opp_run_id",
    "opp_step_skill", "workflow_id", "opportunity_id",
})
#: What a turn's `origin_ref` keeps, for the same reason (the channel's own keys can carry a
#: subject line or a file's name, and `attachments` does).
_ORIGIN_REF_KEPT = frozenset({"thread_key", "chat_session_id", "client_id", "trigger_id", "on_behalf_of"})


class BytesRemain(Exception):
    """The store refused to delete an attachment. Objects already deleted stay deleted; no
    row has been touched, and deleting again finishes the rest."""


def blocker(session) -> str | None:
    """Why this session's content cannot be deleted right now, or None. A live turn would
    go on writing into the row, and a move would carry the words to another box."""
    if (Turn.objects.filter(sensitivity.turns_of_q(session.pk), status__in=list(Turn.NON_TERMINAL)).exists()
            or session_state.is_running(session)):
        return RUNNING
    if (TransferRequest.objects.filter(session=session, status=TransferRequest.PENDING).exists()
            or SessionHandoff.objects.filter(session=session, settled_at__isnull=True).exists()):
        return MOVING
    forks = Session.objects.filter(parent_session_id=session.pk, content_deleted_at__isnull=True)
    if any(isinstance((meta or {}).get(FORK_KEY), dict) for meta in forks.values_list("metadata", flat=True)):
        return HAS_FORK
    return None


def _delete_bytes(session) -> int:
    """Each attachment's stored objects, before anything else: bytes that outlive their
    row are orphaned for good, while a row that outlives its bytes is deleted again by the
    retry (deleting an object already gone is a no-op). Raises BytesRemain."""
    rows = list(Attachment.objects.filter(session=session).values_list("pk", "storage_key", "sealed_blob_key"))
    if not attachment_storage.is_configured():
        return len(rows)
    for pk, key, sealed_key in rows:
        try:
            for k in (key, sealed_key):
                if k:
                    attachment_storage.delete(k)
        except Exception as exc:  # noqa: BLE001 — reported to the person as a retry, never as done
            logger.warning("could not delete the stored object of attachment %s", pk, exc_info=True)
            raise BytesRemain(str(pk)) from exc
    return len(rows)


def _transcript_ids(session, binding, turn_ids) -> set[str]:
    """Every Claude transcript known to have held this conversation, less any another
    session still uses: dropping that one's ships would take words from somebody else."""
    ids = {session.cli_session_id, binding.transcript_id if binding is not None else ""}
    for sid, cli_sid in Turn.objects.filter(pk__in=turn_ids).values_list("session_id", "cli_session_id"):
        ids |= {sid, cli_sid}
    ids |= set(SessionUsage.objects.filter(session=session).values_list("claude_session", flat=True))
    ids |= set(SessionHandoff.objects.filter(session=session).values_list("source_transcript_id", flat=True))
    ids = {i for i in ids if i}
    if not ids:
        return ids
    live = Session.objects.exclude(pk=session.pk).filter(content_deleted_at__isnull=True)
    ids -= set(RunnerBinding.objects.filter(transcript_id__in=ids, session__in=live)
               .values_list("transcript_id", flat=True))
    ids -= set(live.filter(cli_session_id__in=ids).values_list("cli_session_id", flat=True))
    return ids


def _scrub_turns(session, turn_ids: list, now) -> None:
    """Each turn keeps its row and ids and loses what was said, as a purged turn does
    (`retention.services._scrub_turns`), plus its report title, its product links and what
    its channel kept. Turn by turn only for `origin_ref`, which has to be read."""
    turns = Turn.objects.filter(pk__in=turn_ids)
    for pk, ref in turns.values_list("pk", "origin_ref"):
        kept = {k: v for k, v in (ref or {}).items() if k in _ORIGIN_REF_KEPT}
        if kept != ref:
            Turn.objects.filter(pk=pk).update(origin_ref=kept)
    turns.update(prompt="", result_note="", report_summary="", report_title="", work_product_urls=[],
                 content_purged_at=now)
    TurnEvent.objects.filter(turn_id__in=turn_ids).delete()
    TurnTranscript.objects.filter(turn_id__in=turn_ids).delete()
    CallerToken.objects.filter(turn_id__in=turn_ids).delete()
    AdmissionNotice.objects.filter(turn_id__in=turn_ids).update(detail="", subject="", body="")
    ActAsGrant.objects.filter(turn_id__in=turn_ids).update(reason="")
    # "The latest raw note, verbatim", when the latest failure was one of these turns.
    # Read in Python: `turn_ids` is a JSON list, and containment is not portable.
    hexes = {str(t) for t in turn_ids}
    for inv in FailureInvestigation.objects.filter(workspace_id=session.workspace_id).exclude(sample_note=""):
        if hexes & {str(t) for t in (inv.turn_ids or [])}:
            FailureInvestigation.objects.filter(pk=inv.pk).update(sample_note="")


def _drop_words(session, *, binding, turn_ids, now) -> dict:
    """Every word of it, under the caller's locks. Returns counts, never content."""
    high = Message.objects.filter(session=session).aggregate(m=Max("turn_index"))["m"]
    counts = {
        "messages": Message.objects.filter(session=session).count(),
        "attachments": Attachment.objects.filter(session=session).count(),
        "turns": len(turn_ids),
    }
    # Attachments first: losing a message would only unbind them. A sensitive file's own
    # post_delete deletes its objects again on commit, which is a no-op now.
    Attachment.objects.filter(session=session).delete()
    Message.objects.filter(session=session).delete()
    # A sealed row's key goes too, so a ciphertext a browser cached opens nowhere new.
    for model in (Draft, PageAction, SessionSecret, ChatKey, SessionKeyWrap, SessionKey,
                  AppViewSubscription, PendingMail):
        model.objects.filter(session=session).delete()
    TransferRequest.objects.filter(session=session).update(brief="", note="")
    HandoffArchive.objects.filter(handoff__session=session).delete()
    SessionHandoff.objects.filter(session=session).update(
        brief="", capture_note="", manifest=None, restore_report=None, close_error="")
    AgentSecretRequest.objects.filter(session=session).update(reason="")
    # A coach asked to review this one session stops at once (`access._review_ask_q` also
    # refuses it). The chat that ask opened is its own conversation, and its own to delete.
    SessionReviewAsk.objects.filter(session=session, revoked_at__isnull=True).update(revoked_at=now)
    ActAsGrant.objects.filter(login_session=session).update(reason="")
    _scrub_turns(session, turn_ids, now)
    if binding is not None:
        RunnerBinding.objects.filter(pk=binding.pk).update(
            tail=[], summary="", pending_question=None, pending_answer=None, subagents=[],
            close_error="", close_error_at=None, stream_desired=False, backfill_requested=False)
    fields = {
        "content_deleted_at": now, "content_purged_at": now, "status": Session.ARCHIVED,
        "title": "", "activity": {}, "activity_keys": "", "page_state": {}, "origin_resource": "",
        "page_actions_available": [], "finish_push_due_at": None,
        "metadata": {k: v for k, v in (session.metadata or {}).items() if k in _METADATA_KEPT},
    }
    if high is not None:
        # The retention floor as well: below it no row is written, by any path.
        fields["retention_floor_index"] = max(session.retention_floor_index, high + 1)
    # `.update()`: a deletion is not activity, and its events are said just below.
    Session.objects.filter(pk=session.pk).update(**fields)
    return counts


def delete_content(session, *, by) -> dict:
    """Delete `session`'s stored words and leave the tombstone, in one transaction, under
    the binding and session locks every transcript write takes. `by` is the person (None for
    `absorb`). Returns counts. The caller has asked `access` and `blocker`; a turn that
    started since is refused here by the same `blocker`, as ValueError."""
    attachments_gone = _delete_bytes(session)
    with transaction.atomic():
        binding = RunnerBinding.objects.select_for_update().filter(session=session).first()
        locked = Session.objects.select_for_update().get(pk=session.pk)
        if locked.content_deleted_at is not None:
            return {"messages": 0, "attachments": 0, "turns": 0}
        if (why := blocker(locked)) is not None:
            raise ValueError(why)
        now = timezone.now()
        was_active = locked.status != Session.ARCHIVED
        turn_ids = list(Turn.objects.filter(sensitivity.turns_of_q(locked.pk)).values_list("pk", flat=True))
        transcripts = _transcript_ids(locked, binding, turn_ids)
        counts = _drop_words(locked, binding=binding, turn_ids=turn_ids, now=now)
        counts["attachments"] = max(counts["attachments"], attachments_gone)
        DeletedTranscript.objects.bulk_create(
            [DeletedTranscript(transcript_id=t[:100], session_id=locked.pk) for t in transcripts],
            ignore_conflicts=True)
        with lifecycle.acting(by=by):
            if was_active:
                lifecycle.record_many(lifecycle.ARCHIVED, [locked.pk], reason="its content was deleted",
                                      change={"field": "status", "before": Session.ACTIVE,
                                              "after": Session.ARCHIVED})
            lifecycle.record(lifecycle.CONTENT_DELETED, locked)
        session_content_deleted.send(sender=Session, session=locked, turn_ids=turn_ids, by=by)

        def _tell_open_tabs():
            # A tab holding the conversation drops it: a fresh, empty snapshot.
            groups.publish(groups.session_group(locked.pk), {"type": "session.rebuilt"})
            chat_services.recheck_sockets(locked.pk)

        transaction.on_commit(_tell_open_tabs)
    return counts


def absorb(session) -> None:
    """A deleted transcript was shipped into `session`, a row other than the one deleted
    (`transcript_ingest.refuses_ship`). When that transcript is all it could hold, it is the
    deleted conversation under a new row, so it is held to the same tombstone: its tail and
    title stop showing it. One already holding rows of its own keeps them; only the ship drops."""
    if Message.objects.filter(session=session).exists():
        return
    try:
        delete_content(session, by=None)
    except (ValueError, BytesRemain):
        # Working right now: the ship is dropped anyway, and the next one asks again.
        logger.info("session %s holds a deleted transcript and is busy; left for the next ship", session.pk)

"""Chat sessions: a session's lifecycle.

What a host may write on it, creating
one, whether its transcript is its record, and resetting it. The rest of the
chat machinery is split by responsibility: reading a transcript
(`transcript_read`), recording one (`transcript_ingest`), choosing and moving
runners (`placement`), sending (`send`) and controlling a live session
(`control`).
"""
from __future__ import annotations

import logging

from django.conf import settings
from django.db import transaction

from apps.harness import ledger, provenance, sensitivity
from apps.harness.models import Turn, TurnEvent, TurnTranscript
from apps.realtime import groups

from . import activity as session_activity
from . import attachment_storage, conversation_keys, drafts
from . import transcript_read
from .models import Attachment, Message, RunnerBinding, Session

logger = logging.getLogger(__name__)

#: Session metadata keys canopy itself writes and ACTS on. A caller — a user, a
#: contact, a host — may never set one: `slack_*` decide which Slack channel an
#: agent's replies are posted into, `email_thread_key` decides which session an
#: inbound email thread is delivered to, `transcript_sourced` where the durable
#: record comes from, `embed_app` which host a conversation belongs to. Accepting
#: them from a request let a caller aim the relay at an arbitrary channel or
#: capture someone else's email thread. Spelled as strings (their owners are in
#: other apps); tests/test_session_metadata.py pins each against its owner.
SERVER_OWNED_METADATA = frozenset({
    "embed_app", "requested_runner_id", "transcript_sourced",
    "slack_thread", "slack_team", "slack_channel", "slack_thread_ts",
    # History of detached threads (apps/slack/share.py); retention classes on it.
    "slack_detached",
    "email_thread_key", "via", "capability",
    # A host's runner requirements (`sensitive_data`, apps/harness/runner_requirements.py),
    # copied from the token that started or sent into the session.
    "runner_requirements",
    # MCP Apps receipts + model context (apps/mcp_apps_host/mcp_apps_views.py): canopy's
    # own record of what a View did — a host must never be able to write it.
    "mcp_apps",
    # The resource links a host's tools returned through the gateway, and which tool
    # (apps/host_grants/host_gateway.py `remember_links`): what `site_read` may read back.
    "host_links",
    # Where each confined turn's transcript sits in the session
    # (apps/harness/session_sync.py::TURN_END_EPOCHS).
    "turn_end_epochs",
    # A fork's cut and the directory it runs in (apps/canopy_sessions/placement.py::FORK_KEY).
    "fork",
})

#: The metadata keys a caller MAY set — an allowlist, so a key canopy starts acting on
#: later is canopy's until it is added here on purpose. The host linkage the session list
#: filters by (`host_filters`: `source`, `origin_key`, `opp_slug`, `opp_run_id`) and
#: ace-web's `opp_step_skill`, an embedding page's `workflow_id` / `opportunity_id`;
#: anything else a host wants to keep goes under `x_`, a
#: namespace canopy never reads (tests/test_session_metadata.py checks no app does).
HOST_METADATA_KEYS = frozenset({"source", "origin_key", "opp_slug", "opp_run_id", "opp_step_skill",
                                "workflow_id", "opportunity_id"})
HOST_METADATA_PREFIX = "x_"


def is_host_key(key) -> bool:
    """Whether a caller may set (and read back) metadata `key`."""
    key = str(key)
    return key not in SERVER_OWNED_METADATA and (
        key in HOST_METADATA_KEYS or key.startswith(HOST_METADATA_PREFIX))


MAX_HOST_METADATA_KEYS = 20
MAX_HOST_METADATA_BYTES = 4096


def host_metadata(raw) -> dict:
    """What a caller may put on a session: their own descriptive keys (ace-web's
    `origin_key`, `opp_slug`, …, `HOST_METADATA_KEYS` and the `x_` namespace), never
    one canopy acts on; any other key is dropped. ONE rule for every principal — a
    signed-in user, a PAT, a widget contact — so a contact's conversation carries a
    host's link exactly as a user's does."""
    import json

    if not isinstance(raw, dict):
        return {}
    out = {str(k): v for k, v in raw.items() if is_host_key(k)}
    if len(out) > MAX_HOST_METADATA_KEYS or len(json.dumps(out, default=str)) > MAX_HOST_METADATA_BYTES:
        raise ValueError(f"session metadata is limited to {MAX_HOST_METADATA_KEYS} keys "
                         f"and {MAX_HOST_METADATA_BYTES} bytes")
    return out


#: Where `stamp_host_metadata` records which embedding site a session belongs to.
EMBED_APP_KEY = "embed_app"


def stamp_host_metadata(metadata: dict, request) -> dict:
    """Stamp the SERVER-OWNED host keys onto a new session's `metadata`, from the
    request's delegated token — never from the body: `embed_app` (which site
    created it, so one host's widget cannot create or list under another's name)
    and the site's runner requirements (`sensitive_data`). A request with no acting site (a
    browser, a PAT) stamps nothing. One rule for a user's and a contact's
    session."""
    from apps.harness import runner_requirements as rr

    app = getattr(request, "delegated_app", None)
    if app is None:
        return metadata
    metadata[EMBED_APP_KEY] = app.name
    reqs = getattr(request, "runner_requirements", ())
    if reqs:
        metadata[rr.METADATA_KEY] = list(reqs)
    return metadata


def host_filters(rows, *, source: str = "", origin_key: str = "", opp_slug: str = "",
                 opp_run_id: str = "", resource: str = "", page_path: str = ""):
    """Narrow a session queryset by the filters a host lists with. Applied AFTER
    each door's own ACL query, so a filter only ever narrows what that door
    already allows. Empty string = no filter.

    `source` / `origin_key` / `opp_*` match the host's own `metadata` (opaque to
    canopy: `origin_key` is an embedder's tenant, stamped at create). `resource`
    / `page_path` match the session's declared page state (page_state.py) — "the
    conversations I had on THIS page"; a session that declared no page matches
    neither, rather than canopy guessing provenance from a title."""
    for field, value in (("metadata__source", source), ("metadata__origin_key", origin_key),
                         ("metadata__opp_slug", opp_slug), ("metadata__opp_run_id", opp_run_id),
                         ("page_state__resource", resource), ("page_state__path", page_path)):
        if value:
            rows = rows.filter(**{field: value})
    return rows


def _merge_metadata(session: Session, updates: dict) -> None:
    """Write `updates` into the session's metadata as it is IN THE ROW, not as
    the caller's copy remembers it. A read-modify-write of a stale in-memory
    copy would put back whatever it loaded — dropping, say, a
    `runner_requirements` a concurrent send stamped after it was read."""
    with transaction.atomic():
        locked = Session.objects.select_for_update().get(pk=session.pk)
        locked.metadata = {**(locked.metadata or {}), **updates}
        locked.save(update_fields=["metadata", "updated_at"])
    session.metadata = locked.metadata
    session.updated_at = locked.updated_at


def add_runner_requirements(session: Session, reqs, *, refuse_keyless: bool = False) -> None:
    """Union `reqs` into the session's requirements. Never removes one: a
    conversation that held a host's data keeps the host's floor, whatever token
    touches it next (spec 2026-09-30-zdr-runners).

    `refuse_keyless` (a person's send, `doors.send_from`): when this would make the
    conversation sensitive and it can get no key — its starter has no vault key, or it
    is a contact's — raise `conversation_keys.CannotHold` and change nothing. All or
    nothing: the switch, the sealing of its history and the refusal never land apart,
    so a refused send never leaves an older conversation private with its past
    blanked."""
    from apps.harness import runner_requirements as rr

    reqs = set(reqs or ())
    if not reqs:
        return
    with transaction.atomic():
        s = Session.objects.select_for_update().get(pk=session.pk)
        meta = dict(s.metadata or {})
        # A malformed stored value reads as {UNSATISFIABLE}; the union keeps it,
        # so the session stays unclaimable — the fail-closed answer.
        current = set(rr.requirements_of_session(s))
        merged = sorted(current | reqs)
        became_sensitive = False
        if merged != sorted(current):
            was_sensitive = sensitivity.is_sensitive(s)
            meta[rr.METADATA_KEY] = merged
            s.metadata = meta
            became_sensitive = not was_sensitive and sensitivity.is_sensitive(s)
            # A sensitive conversation keeps no title: one typed or derived before it
            # held the host's data may already quote it.
            fields = {"metadata": meta, **({"title": ""} if became_sensitive else {})}
            Session.objects.filter(pk=s.pk).update(**fields)
            if became_sensitive:
                # Its words are sealed from now on, to a key made for its people
                # (`conversation_keys.ensure_key`; a starter with no vault key gets
                # none, and the send door refuses them).
                key = conversation_keys.ensure_key(s)
                if key is None and refuse_keyless:
                    # Rolls the whole switch back (this atomic block).
                    raise conversation_keys.CannotHold()
                # What it said before is held to the same rule as what it says next,
                # in this transaction: its rows sealed, its turns' words withheld.
                conversation_keys.seal_existing(s)
                _withhold_what_it_kept(s)
                # A file attached before is no more sealed than its words were: it goes
                # (rows now, stored bytes once this commits), so nothing serves it on.
                _drop_attachments(s)
                # What its people had typed and not sent stays in their browsers now.
                drafts.forget_words(s)
                # Every open socket on it re-asks `reads_content`: a token's, or a
                # co-tenant's who is not in it, closes now rather than reading on.
                transaction.on_commit(lambda pk=s.pk: recheck_sockets(pk))
                # A fork made while it was ordinary holds a copy of the same words:
                # it is made private with it, in this transaction (its rows sealed to
                # the key its forker carries on, `conversation_keys.ensure_key`).
                for child in Session.objects.filter(parent_session_id=s.pk).only("id", "metadata"):
                    if isinstance((child.metadata or {}).get("fork"), dict):
                        add_runner_requirements(child, merged)
    # Either way the caller's copy says what the row says, so a decision read off
    # it next (`sensitivity.seals`) is never made on a stale instance.
    session.metadata = meta
    if became_sensitive:
        session.title = ""


def _withhold_what_it_kept(session: Session) -> None:
    """A conversation that just became sensitive, held to what a sensitive one keeps
    (docs/architecture/tenancy.md § Sensitive conversations) for everything it kept
    before: an ended turn's prompt wiped and its note withheld (a live turn keeps
    its prompt until it ends, `closing.close_turn`); every stored event reduced to
    its metadata and a tool's result to its call; no raw transcript; the binding's
    tail, summary and pending dialog's words gone. Its Message rows are sealed
    beside this (`conversation_keys.seal_existing`)."""
    # Every turn whose conversation this is: a chat turn's, and an agent, board or email
    # turn's that ran in it (`sensitivity.conversation_of`).
    of_it = sensitivity.turns_of_q(session.pk)
    turn_ids = list(Turn.objects.filter(of_it).values_list("pk", flat=True))
    turns = Turn.objects.filter(pk__in=turn_ids)
    for turn in turns.filter(status__in=list(Turn.TERMINAL)).exclude(prompt="", result_note=""):
        Turn.objects.filter(pk=turn.pk).update(
            prompt="", result_note=sensitivity.withheld_note(turn.result_note))
    events = []
    # Locked against an appender racing the switch: an event written as it happens
    # either lands before (and is reduced here) or after (and is stored as metadata,
    # `ledger.append_events` locks the same turn rows).
    list(Turn.objects.select_for_update().filter(pk__in=turn_ids).values_list("pk", flat=True))
    for e in TurnEvent.objects.filter(turn_id__in=turn_ids).iterator():
        payload = e.payload if isinstance(e.payload, dict) else {}
        if payload.get("redacted") == sensitivity.REDACTED:
            continue
        if e.kind in sensitivity.RESULT_KINDS:
            payload = sensitivity.strip_result(payload, session=session)
        e.payload = ledger.metadata_of({"kind": e.kind, "payload": payload})
        events.append(e)
    if events:
        TurnEvent.objects.bulk_update(events, ["payload"], batch_size=500)
    TurnTranscript.objects.filter(turn_id__in=turn_ids).delete()
    binding = RunnerBinding.objects.filter(session_id=session.pk).first()
    if binding is not None:
        RunnerBinding.objects.filter(pk=binding.pk).update(
            tail=[], summary="", pending_question=sensitivity.menu_metadata(binding.pending_question))


def _drop_attachments(session: Session) -> int:
    """Every file attached to `session`, sent or not, deleted: the rows in this
    transaction and the stored bytes after it commits (best-effort; a failed delete is
    logged, the row is gone either way, so no route serves it). A private
    conversation keeps no files (`api.upload_attachment` refuses one), because the box
    that reads a file holds no key to seal it. Returns how many."""
    rows = list(Attachment.objects.filter(session_id=session.pk).values_list("pk", "storage_key"))
    if not rows:
        return 0
    Attachment.objects.filter(pk__in=[pk for pk, _ in rows]).delete()
    keys = [k for _, k in rows if k]

    def _delete_bytes():
        if not attachment_storage.is_configured():
            return
        for key in keys:
            try:
                attachment_storage.delete(key)
            except Exception:  # noqa: BLE001 — the row is gone; bytes are logged for cleanup
                logger.warning("could not delete a private conversation's attachment %s", key,
                               exc_info=True)

    transaction.on_commit(_delete_bytes)
    return len(rows)


def recheck_sockets(session_id, *, user_ids=()) -> None:
    """Ask the chat sockets open on one session to re-check who may read it
    (`consumers.SessionConsumer.access_recheck`): every socket on it, or only
    those `user_ids` hold. Best-effort — a socket also re-checks on every act."""
    frame = {"type": "access.recheck", "session_id": str(session_id)}
    targets = ([groups.chat_user_group(uid) for uid in user_ids] if user_ids
               else [groups.session_group(session_id)])
    for group in targets:
        try:
            groups.publish(group, frame)
        except Exception:  # noqa: BLE001 — never fail the write that asked
            logger.warning("could not ask %s to re-check", group, exc_info=True)


def create_session(*, workspace, created_by=None, agent=None, project: str = "", title: str = "",
                   metadata: dict | None = None, contact=None, parent=None,
                   conversation_key: dict | None = None) -> Session:
    """One way to start a conversation, whoever starts it. A USER owns it (a
    participant row, SP3 multiplayer); a CONTACT has no account, so the session
    records them on `contact` instead and has no creator — which is what keeps
    it out of every member's list (`access.contact_session_q`)."""
    # Atomic so a session never exists without its owner. Local imports avoid a cycle.
    from .models import SessionParticipant
    from .participants import ensure_participant

    meta = dict(metadata or {})
    # A sensitive conversation keeps no title, from its first row: whoever starts
    # it may name it after the host's data.
    if sensitivity.is_sensitive(Session(metadata=meta)):
        title = ""
    # A real runner will drive this session in emdash, so its transcript is the
    # record — stamped at birth, never inferred later, so a session can't change
    # its mind about where its history lives (see `transcript_sourced`). Under the
    # dev stub there is no emdash session and no transcript, so the ledger stays
    # the source.
    if not getattr(settings, "CHAT_STUB_EXECUTOR", True):
        meta.setdefault(TRANSCRIPT_SOURCED, True)
    with transaction.atomic():
        session = Session(
            workspace=workspace, agent=agent, project=project, created_by=created_by,
            title=title, metadata=meta, contact=contact,
            # What opened it (request + `parent`): apps/harness/provenance.py.
            **provenance.session_fields(parent=parent),
        )
        # A key pair its starter's browser made (`conversation_keys.check_offer`), kept by
        # the birth signal in place of one made here.
        session._offered_key = conversation_key
        session.save(force_insert=True)
        if created_by is not None:
            ensure_participant(session, created_by, SessionParticipant.OWNER)
            _inherit_participants(session, created_by)
    return session


def _inherit_participants(session: Session, created_by) -> None:
    """A session dispatched from inside another one is work done on behalf of
    that conversation's people, so they join it: an agent asked to start the
    work in chat creates it under its own login, and without this the person
    who asked could not see what they asked for.

    Only from a parent the creator can read (naming a session id is not a way
    to pull its people in), only people who are members of the new session's
    workspace, and never as owner — the dispatcher owns what it started; a
    parent viewer stays a viewer.

    The person who asked joins too, even when the parent has no participant
    rows: a session started in emdash is runner-discovered, so its person
    reads it through the tenant leg and typing into it never writes a row
    (#926). The asker is the parent TURN's initiator — only a turn in that
    same parent, so naming some other turn is not a way in either
    (2026-10-19: ACE's handover of a run from emdash task `fi` reached no one)."""
    from apps.workspaces import services as wsvc

    from . import access
    from .models import SessionParticipant
    from .participants import ensure_participant

    parent = session.parent_session
    if parent is None or not access.can_read(created_by, parent):
        return
    # A private parent's people come along only for one of them: seeing it (an agent's
    # admin) is not being in it (`sensitivity.is_conversation_person`).
    if sensitivity.is_sensitive(parent) and not sensitivity.is_conversation_person(parent, user=created_by):
        return
    for p in parent.participants.select_related("user").exclude(user=created_by):
        if not wsvc.is_member(p.user, session.workspace_id):
            continue
        role = SessionParticipant.VIEWER if p.role == SessionParticipant.VIEWER else SessionParticipant.EDITOR
        ensure_participant(session, p.user, role)
    turn = session.parent_turn
    asker = turn.initiator_user if turn is not None and turn.chat_session_id == parent.pk else None
    if asker is not None and asker != created_by and wsvc.is_member(asker, session.workspace_id):
        ensure_participant(session, asker, SessionParticipant.EDITOR)


# Marks a session whose DURABLE record is its Claude transcript, keyed on each
# record's ordinal — as opposed to the ledger projection, which only ever captures
# what happened inside a Turn. Stamped at creation when a real runner will execute
# the session (see `create_session`); runner-discovered sessions qualify by
# construction. See `transcript_sourced`.
TRANSCRIPT_SOURCED = "transcript_sourced"


def transcript_sourced(session) -> bool:
    """True when this session's durable messages come from its transcript.

    ONE rule for both kinds of session — where a conversation ORIGINATED (a phone
    composer vs a task discovered in emdash) says nothing about where its record
    should live, and treating it as if it did is what split the two paths:

      - transcript-sourced: every record in the emdash session becomes a Message,
        keyed on its transcript ordinal, whether or not a Turn was open. Covers
        text you type directly in emdash and text the agent writes after handing
        the floor back (a background job finishing), neither of which sits inside
        a turn.
      - ledger-sourced (the fallback): Messages are projected from a Turn's events.
        Only for sessions no runner will ever execute — the dev stub, where there
        IS no transcript to read.

    Sessions created before the unification carry no flag and stay ledger-sourced
    until reset: their rows are numbered by a dense counter (0,1,2…) which would
    collide with transcript ordinals in the same `turn_index` column, so nothing
    switches one implicitly. `manage.py reset_chat_state` moves them over in bulk —
    cheap, because for a bound session these rows are a CACHE of the transcript,
    not an archive.
    """
    if session.origin == Session.ORIGIN_RUNNER:
        return True  # discovered in emdash: the transcript is all there ever was
    return bool((session.metadata or {}).get(TRANSCRIPT_SOURCED))


# Why a reset can be refused. The UI renders these, so they are stable strings.
RESET_OK = "ok"
RESET_NO_BINDING = "no_binding"            # nothing knows which box/worktree it came from
RESET_RUNNER_UNREACHABLE = "runner_unreachable"   # transient: retry when it's back
RESET_SENSITIVE_BOX = "sensitive_data_runner"     # its box never ships a transcript


def _reset_blocker(session) -> tuple[str, object]:
    """(reason, binding) — RESET_OK when this session's rows can be re-derived.

    Deliberately NOT "is the emdash task still open?". A backfill resolves the
    transcript by WORKTREE PATH under ~/.claude/projects, never by asking emdash,
    and Claude Code never deletes those files — so a task emdash deleted months
    ago still ships its full history (verified against the live fleet 2026-07-26:
    tasks absent from emdash's DB entirely, transcripts resolved, 545 and 607
    records). Falling off the session report ends a session's LISTING, not its
    recoverability; conflating the two is what made this look dangerous.

    What actually blocks a reset is having no pointer to a transcript at all (no
    binding), or no live runner to read it (offline/retired — transient).
    """
    binding = getattr(session, "runner_binding", None)
    if binding is None or binding.runner_id is None:
        return RESET_NO_BINDING, None
    # Reachable is enough to READ A FILE — mirrors request_backfill, which never
    # needs emdash's CDP port.
    if transcript_read.backfill_refused(binding):
        # Dropping the rows would lose them: a sensitive-data box ships nothing back.
        return RESET_SENSITIVE_BOX, binding
    if not binding.runner.is_reachable:
        return RESET_RUNNER_UNREACHABLE, binding
    return RESET_OK, binding


def reset_session(session, *, dry_run: bool = False) -> dict:
    """Drop one session's derived rows and re-derive them from its transcript.

    The rows are a CACHE of a file on the runner's disk, so this is cheap and
    repeatable — the operation you want constantly while building, not a migration
    to be performed once with ceremony. Returns a result dict rather than raising,
    so a bulk caller can report per-session outcomes.
    """
    reason, binding = _reset_blocker(session)
    rows = Message.objects.filter(session=session).count()
    out = {
        "session_id": str(session.id),
        "title": session.title,
        "ok": reason == RESET_OK,
        "reason": reason,
        "rows_dropped": rows if reason == RESET_OK else 0,
        "runner": binding.runner.name if (binding and binding.runner_id) else "",
    }
    if reason != RESET_OK or dry_run:
        return out
    Message.objects.filter(session=session).delete()
    session_activity.rebuild(session)   # counters restart; the re-derive refolds them
    _merge_metadata(session, {TRANSCRIPT_SOURCED: True})
    transcript_read.request_backfill(session)
    return out


def reset_sessions(sessions, *, prune_ghosts: bool = False, dry_run: bool = False) -> dict:
    """Bulk reset. `sessions` is any Session iterable/queryset already scoped by
    the caller (a workspace, a tenant, one id) — this never widens it.

    `prune_ghosts` DELETES runner-origin sessions that have no binding: a
    discovered session with no pointer to a transcript can't be shown or rebuilt,
    and the next session report re-creates it if its task is still open. Web
    sessions are never pruned — a chat you started is not something to garbage
    collect.
    """
    results, pruned = [], []
    for session in sessions:
        result = reset_session(session, dry_run=dry_run)
        if (
            prune_ghosts
            and result["reason"] == RESET_NO_BINDING
            and session.origin == Session.ORIGIN_RUNNER
        ):
            pruned.append({"session_id": str(session.id), "title": session.title})
            if not dry_run:
                session.delete()
            continue
        results.append(result)
    return {
        "dry_run": dry_run,
        "reset": [r for r in results if r["ok"]],
        "skipped": [r for r in results if not r["ok"]],
        "pruned": pruned,
        "rows_dropped": sum(r["rows_dropped"] for r in results),
    }

"""Sending into a chat: staged attachments, a send's origin, the ledger and
transcript-sourced send paths (`send_message`), the queued list, withdrawing
or re-placing a queued send, and the dev stub's inline executor. Doors call
`doors.send_from`, not this module.
"""
from __future__ import annotations

import datetime as _dt
import uuid

from django.conf import settings
from django.db import IntegrityError, transaction
from django.utils import timezone

from apps.harness import sensitivity
from apps.harness import services as harness_services
from apps.harness.models import Turn

from . import authorship, conversation_keys, services, transcript_ingest, transcript_read
from . import placement as session_placement
from .models import Message, RunnerBinding, Session


def _contact_of(initiator):
    """The contact behind a send, when it is one (`harness.initiator`)."""
    return getattr(initiator, "contact", None)


def claim_pending_attachments(session, message=None, user=None) -> list[dict]:
    """Mark the SENDER's un-sent attachments on this session as sent, and
    describe them for the runner.

    Swept off the session rather than passed by id, so the WebSocket `chat.send`
    frame needs no new field and REST and WS behave identically. Scoped to
    `uploaded_by=user` because it matches the draft model: everyone composes in
    their own box (spec 2026-09-26), so an attachment belongs to its uploader's
    next send — never to a teammate who happens to press Send first. With no
    user (a contact send) nothing is claimed: contacts cannot upload, and they
    must not sweep up a member's half-composed attachments.

    `message` is None for a runner-origin session, which writes no user Message
    row — hence the sent_at stamp, without which those rows would ride along on
    every later send too.
    """
    from .models import Attachment

    if user is None or getattr(user, "pk", None) is None:
        return []
    # A private conversation takes no files (the box reading one holds no key to it):
    # whatever is pending was attached before it became private and is not sent.
    if sensitivity.is_sensitive(session):
        return []
    pending = list(Attachment.objects.filter(
        session=session, uploaded_by=user, sent_at__isnull=True))
    if not pending:
        return []
    now = timezone.now()
    Attachment.objects.filter(pk__in=[a.pk for a in pending]).update(
        sent_at=now, **({"message": message} if message is not None else {})
    )
    return [
        {"id": str(a.id), "filename": a.filename, "content_type": a.content_type}
        for a in pending
    ]


def sweep_orphaned_attachments(*, older_than: _dt.timedelta) -> int:
    """Delete attachments that never got claimed by a send. Returns the count.

    An unbound row (`sent_at IS NULL`) past `older_than` is one of two things:
    the tab closed before sending, or a send raced the upload and landed before
    `claim_pending_attachments` had a row to sweep (the composer now blocks
    sending while an upload is in flight, see SendBox's `attachmentsUploading` —
    this is cleanup for whatever a send already got past that gate before the
    fix, plus the ordinary abandoned-tab case going forward). Either way it is
    never getting bound; the bytes are deleted before the row, best-effort — a
    row that outlives a failed byte-delete is recoverable later, a blob that
    outlives its row is not.
    """
    from . import attachment_storage
    from .models import Attachment

    cutoff = timezone.now() - older_than
    qs = Attachment.objects.filter(sent_at__isnull=True, created_at__lt=cutoff)
    configured = attachment_storage.is_configured()
    count = 0
    for row in qs:
        if configured:
            try:
                attachment_storage.delete(row.storage_key)
            except Exception:
                transcript_read.logger.warning(
                    "sweep: could not delete bytes for attachment %s; deleting the row anyway",
                    row.id, exc_info=True,
                )
        row.delete()
        count += 1
    return count


# Which product a session BELONGS to, keyed off the marker its creator stamped.
# `metadata.source` is already the canonical "who made this" marker — canopy's
# own session LIST filters on it (`?source=ace-web`).
SOURCE_ORIGINS = {"ace-web": Turn.ORIGIN_ACE_WEB}


def default_origin(session) -> str:
    """The source a turn on this session is, when the caller didn't name one.

    A session ace-web created is ace-web work — whoever typed it, over whatever
    transport. That is the rule, and getting it wrong is what shipped in #496:
    origin was threaded only through ace-web's SERVER-side run dispatcher, on
    the reasoning that a human typing "IS a human typing" and therefore chat. So
    a person typing into ace-web's own chat produced `canopy_web_chat`,
    indistinguishable from canopy's chat UI, and routed to whatever runs canopy
    chat rather than to the box that runs ace-web's work. Observed directly:
    "testing", typed into an ace-web session, went to a laptop runner.

    The distinction that matters is not human-vs-programmatic, it is WHICH
    PRODUCT the work belongs to. Deriving it here rather than asking each caller
    to pass it means every ace-web surface is covered by construction — the chat
    UI over the WS, the workbench's discuss-this-step pane, and the run
    dispatcher — with no client change and no way for one of them to be missed.

    Not tamper-proof, and deliberately not sold as such: `metadata` is
    caller-supplied on the generic session-create endpoint, so a caller could
    stamp `source: ace-web` itself. That is no weaker than what already exists —
    `ace_web` is a POSTABLE origin any caller may name outright — and the blast
    radius is the one the routing spec already accepted: you can only enqueue
    into your own workspace, and a rule only redirects that agent's work among
    runners you can see.
    """
    source = (getattr(session, "metadata", None) or {}).get("source")
    return SOURCE_ORIGINS.get(source, Turn.ORIGIN_CANOPY_WEB_CHAT)


def _merge_origin_ref(extra: dict | None, *, thread_key: str, session: Session) -> dict:
    """The turn's `origin_ref`: what the CHANNEL needs to remember about the ask,
    under what the harness needs to route it.

    A channel reacts to a turn from a signal, holding nothing but the turn — so
    anything it will need then has to be written onto the turn now. Slack is the
    case in point: a slash command's status line adopts the message the command
    already posted, and that message's ts existed only on the in-memory request.

    The harness's keys are written LAST so a channel can never overwrite the
    thread or session a turn routes by.
    """
    return {**(extra or {}), "thread_key": thread_key, "chat_session_id": str(session.id)}


def send_message(
    *, session: Session, text: str, user, client_id: str = "", placement: str | None = None,
    origin: str | None = None, initiator=None, origin_ref: dict | None = None,
    capability: str | None = None, parent=None,
) -> tuple[Message, Turn]:
    """`_send_message`, plus the `chat.message` analytics fact — once per send,
    whichever of its two paths (ledger or transcript-sourced) carried it, and never
    for a retried send that collapsed onto an existing turn."""
    from apps.events import activity

    message, turn = _send_message(
        session=session, text=text, user=user, client_id=client_id, placement=placement,
        origin=origin, initiator=initiator, origin_ref=origin_ref, capability=capability,
        parent=parent,
    )
    if turn is not None:
        activity.emit(activity.CHAT_MESSAGE, workspace=session.workspace_id, actor=user,
                      agent=session.agent, surface=turn.origin, ref=str(turn.pk), dedupe=True,
                      project=activity.repo_of(session=session))
    return message, turn


def _send_message(
    *, session: Session, text: str, user, client_id: str = "", placement: str | None = None,
    origin: str | None = None, initiator=None, origin_ref: dict | None = None,
    capability: str | None = None, parent=None,
) -> tuple[Message, Turn]:
    """Record the human's message and enqueue the session Turn that answers it.

    Idempotency: pass a stable `client_id` (a client-generated nonce) to make a
    retried/double-submitted send collapse onto the SAME user Message + Turn.
    Without one, the key falls back to the message's session index — best-effort
    only (a genuine retry after the first commit would compute a new index), so a
    nonce is required for true double-submit safety.

    `placement`: "wait" pins to the session's currently bound runner; a runner
    UUID string pins to that runner outright; None leaves normal routing/
    stickiness in charge (including, for the FIRST send of an unbound directed
    new chat, the `requested_runner_id` stashed at create time). See
    `_resolve_placement`.

    `origin`: which SOURCE of work this send is, the key source-aware routing
    composes a runner list on (spec 2026-07-27). None means the default — a
    human typing in canopy's own chat UI. ace-web passes `ace_web` so its
    delegated runs can be routed (and read) as what they are rather than
    disappearing into the chat source.

    For an origin=runner session the TRANSCRIPT is the sole durable source
    (spec 2026-07-24): the user's words reach the DB when the runner ships the
    transcript record they became, keyed on its ordinal. Persisting a second
    copy here (keyed _next_index) would collide index spaces and duplicate the
    send, so this path writes no row — the frontend already echoes the message
    optimistically (draft.committed), and a transient Message keeps the contract.
    """
    origin = origin or default_origin(session)
    if services.transcript_sourced(session):
        return _send_transcript_sourced_message(
            session=session, text=text, user=user, client_id=client_id,
            placement=placement, origin=origin, initiator=initiator,
            origin_ref=origin_ref, capability=capability, parent=parent,
        )
    with transaction.atomic():
        locked = Session.objects.select_for_update().get(pk=session.pk)
        if client_id:
            existing = Message.objects.filter(
                session=session, role=Message.USER, content__client_id=client_id
            ).first()
            if existing is not None:
                key = f"chat:{session.id.hex}:{client_id}"
                turn = Turn.keyed(key, Turn.scope_for(session=session)).first()
                return existing, turn
        index = transcript_ingest._next_index(session)
        from apps.harness import sensitivity

        # A sensitive conversation's row says THAT the person spoke (metadata) and
        # keeps the words sealed, for the conversation's own people
        # (`sensitivity.seal`); the turn's prompt is wiped when it ends. Asked of
        # the LOCKED row: the caller's copy may predate a requirement added since.
        sensitive = sensitivity.seals(locked)
        if sensitive:
            # Sealed to the conversation's key, which only its people hold: no key
            # (a contact, a starter with no vault key) is a refusal, never words kept
            # where nobody could read them back (`conversation_keys.require_key`).
            conversation_keys.require_key(locked, user=user, contact=_contact_of(initiator))
        plaintext, content, sealed = (sensitivity.seal(locked, index, Message.USER, text, {"text": text})
                                      if sensitive else (text, {"text": text}, None))
        if client_id:
            content["client_id"] = client_id
        message = Message.objects.create(
            session=session, turn_index=index, role=Message.USER,
            plaintext=plaintext, content=content, sealed=sealed,
        )
        turn = _enqueue_chat_turn(
            session=session, message=message, text=text, user=user, client_id=client_id,
            fallback_key=str(index), placement=placement, origin=origin, initiator=initiator,
            origin_ref=origin_ref, capability=capability, parent=parent,
        )
        resealed = []
        if not sensitive and sensitivity.seals(locked, turn):
            # The turn was started from a sensitive turn or conversation, and this
            # conversation took on its requirements: the row is sealed before the
            # transaction that wrote it commits, so no reader ever sees it open.
            conversation_keys.require_key(locked, user=user, contact=_contact_of(initiator))
            message.plaintext, message.content, message.sealed = sensitivity.seal(
                locked, index, Message.USER, text, {"text": text})
            if client_id:
                message.content["client_id"] = client_id
            session.metadata = locked.metadata
            resealed = ["plaintext", "content", "sealed"]
        # The ledger path writes its own row, so it records the author directly
        # rather than through the transcript marker.
        if user is not None and getattr(user, "is_authenticated", False):
            message.author = {"name": (user.get_full_name() or "").strip() or user.email,
                              "user_id": user.pk}
        if turn is not None:
            message.source_turn_id = turn.pk
        message.save(update_fields=["author", "source_turn_id", *resealed])
        # Fan the row out to every OTHER watcher on the session socket. The
        # sender's own client already has it (the optimistic echo off
        # `draft.committed` / the REST response), but nobody said so to anyone
        # else: unlike the transcript-sourced path (`post_session_stream`
        # publishes every "user" ledger row live, sender included — the client
        # upserts on turn_index/text so a duplicate collapses instead of
        # doubling), a ledger-sourced send (the dev stub, and any
        # pre-unification session not yet reset) wrote the durable Message
        # directly and published nothing, so a peer's transcript never
        # gained the line until they reloaded. Same frame shape
        # (`stream_map.turn_event_to_frames`'s "user" case), built directly
        # since the real id is already in hand — no ledger round trip needed.
        _publish_user_message(session.id, message, client_id)
    return message, turn


def _enqueue_chat_turn(
    *, session: Session, message: Message | None, text: str, user, client_id: str,
    fallback_key: str, placement: str | None, origin: str, initiator, origin_ref: dict | None,
    capability: str | None, parent,
) -> Turn | None:
    """Enqueue the session Turn that answers one send — shared by the ledger and
    the transcript-sourced paths, which differ only in whether `message` is a
    durable row and in `fallback_key` (the idempotency suffix when the client
    sent no nonce).

    Continuity: every send in a chat reuses ONE emdash session (the runner's
    _thread_key reads this), so a conversation is one durable thread rather than
    a fresh session per message. chat_session_id tells a session-capable runner
    to BRIDGE the emdash response back into the ledger (vs the normal
    fire-and-continue), so the website streams the reply.

    A RUNNER-DISCOVERED session already has a binding keyed `emdash:<task>` (the
    report sweep wrote it). Sending str(session.id) there matched nothing, so
    resolve_session answered new_thread and the runner SPAWNED A FRESH emdash
    session instead of typing into the live one you were looking at. Prefer the
    binding's existing thread_key; web sessions (no binding yet) keep the session
    id, which is what record_session then stores."""
    binding = getattr(session, "runner_binding", None)
    thread_key = binding.thread_key if (binding and binding.thread_key) else str(session.id)
    pinned = session_placement._resolve_placement(session, placement, user)
    ref = _merge_origin_ref(origin_ref, thread_key=thread_key, session=session)
    if client_id:
        # The one place a send's client nonce survives as itself: the
        # idempotency key's suffix is `fallback_key` when there is none, and
        # `queued_messages` must not report that as a client_id.
        ref["client_id"] = client_id
    attachments = claim_pending_attachments(session, message, user)
    if attachments:
        ref["attachments"] = [*(ref.get("attachments") or []), *attachments]
    turn, _created = harness_services.enqueue_turn(
        session=session,
        origin=origin,
        idempotency_key=f"chat:{session.id.hex}:{client_id or fallback_key}",
        prompt=text,
        origin_ref=ref,
        # WHO sent it. Not decoration: this is the actor half of the routing key
        # (spec 2026-09-05), and it is the ONLY place an `ace_web` or
        # `canopy_web_chat` turn can get one — neither carries the
        # `origin_ref["from"]` an email turn is routed by. Left unset, every actor
        # rule on those sources silently matches nothing. `enqueue_turn` ignores an
        # unauthenticated user, so this is safe to pass unconditionally.
        enqueued_by=user,
        pinned_runner=pinned,
        initiator=session_placement._initiator(initiator, user, origin),
        capability=capability,
        parent=parent,
    )
    return turn


def _publish_user_message(session_id, message: Message, client_id: str = "") -> None:
    from apps.realtime.groups import publish, session_group

    payload = {
        "message_id": str(message.pk),
        "turn_index": message.turn_index,
        "plaintext": message.plaintext,
        "author": message.author,
        "client_id": client_id,
    }
    transaction.on_commit(
        lambda: publish(session_group(session_id), {"type": "chat.user_message", "data": payload})
    )


def clear_draft_after_http_send(session: Session, user, text: str) -> None:
    """See `drafts.clear_after_http_send`; publishes the cleared draft to the
    session group in the consumer's own `draft.updated` shape, so the author's
    other tabs reset and every peer's typing row clears."""
    from apps.realtime.groups import publish, session_group

    from . import drafts, serializers

    if user is None or not getattr(user, "is_authenticated", False):
        return
    draft = drafts.clear_after_http_send(session, user, text)
    if draft is None:
        return
    message = {
        "type": "draft.updated", "author_id": user.id,
        "draft": serializers.draft_dto(draft),
        "peer": serializers.peer_draft_dto(draft),
    }
    transaction.on_commit(lambda: publish(session_group(session.id), message))


def queued_messages(session: Session) -> list[dict]:
    """Human sends in this session that have not reached the transcript yet.

    Derived from Turn rows, never stored — the same reasoning as
    harness.turn_status: it is a function of rows that change on their own clock.
    A send leaves the list when the transcript row carrying its turn id lands
    (Message.source_turn_id, parsed from the author marker), or when its turn
    ends without one (cancelled, failed).

    Bounded by the NON-TERMINAL turns, never by the session's whole history:
    this runs on every status transition and every streamed transcript batch,
    so a long-lived session must not make it scan every Message it has ever
    landed. `select_related` on the initiator FKs is load-bearing too — dropped,
    `authorship.author_of` (called once per turn below) turns back into an N+1."""
    turns = list(
        Turn.objects.select_related("initiator_user", "initiator_contact")
        .filter(chat_session=session, status__in=list(Turn.NON_TERMINAL))
        .exclude(initiator_user__isnull=True, initiator_contact__isnull=True)
        .order_by("created_at")
    )
    if not turns:
        return []
    # A person's chat sends only — the same rule the claim marks by. An email or
    # scheduled turn bound to this session is the agent's work, not a line
    # somebody typed.
    turns = [t for t in turns if authorship.is_chat_send(t)]
    if not turns:
        return []
    landed = set(Message.objects.filter(session=session, source_turn_id__in=[t.pk for t in turns])
                 .values_list("source_turn_id", flat=True))
    out = []
    for t in turns:
        if t.pk in landed:
            continue
        # From the turn, not its delivered prompt: a slash command goes
        # unmarked but somebody still typed it.
        author = authorship.author_of(t)
        out.append({
            "turn_id": str(t.pk),
            "client_id": str((t.origin_ref or {}).get("client_id") or ""),
            "author": author,
            "text": t.prompt or "",
            "sent_at": t.created_at.isoformat(),
            "state": "queued" if t.status == Turn.QUEUED else "delivering",
        })
    return out


def unqueue_send(session: Session, turn_id, user, *, by: str = "") -> bool:
    """Take back one of YOUR OWN sends that has not reached the agent yet.
    Returns whether it was withdrawn.

    The single-message counterpart of Stop, which cancels every live turn on the
    session at once. Only a turn still QUEUED qualifies: once a runner has
    claimed it the words may already be in the agent's input, and taking them
    back then would be a claim nobody can keep. Only the sender's own: a
    teammate's queued line is theirs to withdraw. `cancel_turn` guards the
    write on status=QUEUED, so a claim landing in between wins and this
    returns False.
    """
    from apps.harness import services as harness_services

    try:
        uuid.UUID(str(turn_id))
    except (ValueError, AttributeError, TypeError):
        return False
    turn = Turn.objects.filter(pk=turn_id, chat_session=session, status=Turn.QUEUED,
                               initiator_user=user).first()
    if turn is None:
        return False
    if not authorship.is_chat_send(turn):
        return False
    done = harness_services.cancel_turn(turn, by=by)
    if done is None or done.status != Turn.CANCELLED:
        return False
    from .queued_feed import publish_queued

    sid = session.pk
    transaction.on_commit(lambda: publish_queued(sid))
    return True


def place_queued_turn(*, session: Session, placement: str, user=None) -> Turn:
    """Re-pin a session's oldest QUEUED turn — the chat banner's after-the-fact
    directed-placement decision (vs `_resolve_placement`, which only applies to
    a turn being newly enqueued). `placement` is "wait" (pin to the session's
    currently bound runner) or a runner UUID string.

    Raises LookupError if there is no queued turn to place (-> 404), or
    ValueError for an unresolvable placement (-> 422): "wait" with no bound
    runner, an unknown/retired runner id, or a box `user` may not choose
    (`may_place`).
    """
    turn = (
        Turn.objects.filter(chat_session=session, status=Turn.QUEUED)
        .order_by("created_at")
        .first()
    )
    if turn is None:
        raise LookupError("no queued turn to place")
    if placement == "wait":
        binding = getattr(session, "runner_binding", None)
        if not (binding and binding.runner_id):
            raise ValueError("session has no bound runner to wait for")
        turn.pinned_runner_id = binding.runner_id
    else:
        runner = session_placement._placeable_runner(session, placement)
        if runner is None:
            raise session_placement._placement_refused(session, placement, "unknown runner")
        if not session_placement.may_place(user, session, runner):
            raise session_placement._placement_forbidden(session, runner)
        turn.pinned_runner = runner
    turn.save(update_fields=["pinned_runner"])
    return turn


def available_cloud_runner(session: Session, user=None):
    """An ONLINE, session-capable cloud runner this session could be placed on,
    or None. Same `_placeable_runner` gate as every other placement, so the
    answer is never a box that would leave the turn pinned and unclaimable — and
    one `user` (who sent it) may place it on (`may_place`), so the offer is never
    one the click refuses. A
    directed-only box (a test box) is never offered unasked: it takes work only
    when someone chooses it (the pickers still list it)."""
    from apps.harness.models import Runner

    for runner in (Runner.objects.filter(kind=Runner.CLOUD, owner__isnull=False)
                   .exclude(status=Runner.RETIRED).order_by("name")):
        if runner.live_status != Runner.ONLINE or runner.directed_only():
            continue
        if (session_placement._placeable_runner(session, str(runner.id)) is not None
                and session_placement.may_place(user, session, runner)):
            return runner
    return None


MOVE_BRIEF = (
    "{source} was offline with {n} message(s) waiting in this conversation, so it was "
    "moved here. Those messages arrive as the next turn(s) — answer them once you have "
    "checked the state above."
)


def move_queued_turns(*, session: Session, placement: str, user=None, initiator=None) -> list[Turn]:
    """Send a session's QUEUED turns to another runner — the "its box is offline,
    run it somewhere else" move. Returns the turns moved.

    Two cases, because a session that already lives on a box cannot simply have
    its next turn pinned elsewhere (that is `place_queued_turn`, and the
    `transfer_session` docstring records what it cost when done by hand):

    * **Unbound** (a new conversation whose first message never got picked up):
      nothing exists on any box yet, so pinning the waiting turns is the whole
      move, and `requested_runner_id` makes later sends follow.
    * **Bound** to another box: a real transfer, carrying the history and a
      handoff turn. The handoff must run BEFORE the waiting messages — the new
      box is cold, and answering them first is answering blind — and claiming
      is ordered by `created_at`, so the handoff is stamped just ahead of them.

    Raises like `transfer_session`: ValueError (bad target), LookupError
    (nothing queued), RuntimeError (a turn is still executing).
    """
    target = session_placement._placeable_runner(session, placement)
    if target is None:
        raise session_placement._placement_refused(session, placement, "unknown runner")
    if not session_placement.may_place(user, session, target):
        raise session_placement._placement_forbidden(session, target)
    queued = list(Turn.objects.filter(chat_session=session, status=Turn.QUEUED).order_by("created_at"))
    if not queued:
        raise LookupError("no queued turn to move")
    binding = RunnerBinding.objects.select_related("runner").filter(session=session).first()
    if binding is not None and binding.runner_id and binding.runner_id != target.id:
        _binding, handoff = session_placement.transfer_session(
            session=session, placement=str(target.id),
            brief=MOVE_BRIEF.format(source=binding.runner.name, n=len(queued)),
            user=user, initiator=initiator,
        )
        Turn.objects.filter(pk=handoff.pk).update(
            created_at=queued[0].created_at - _dt.timedelta(milliseconds=1))
    else:
        services._merge_metadata(session, {"requested_runner_id": str(target.id)})
    Turn.objects.filter(pk__in=[t.pk for t in queued], status=Turn.QUEUED).update(pinned_runner=target)
    for t in queued:
        t.refresh_from_db()
    return [t for t in queued if t.pinned_runner_id == target.id]


def _send_transcript_sourced_message(
    *, session: Session, text: str, user=None, client_id: str = "",
    placement: str | None = None, origin: str = Turn.ORIGIN_CANOPY_WEB_CHAT,
    initiator=None, origin_ref: dict | None = None, capability: str | None = None,
    parent=None,
) -> tuple[Message, Turn]:
    """The transcript-sourced send path: enqueue the Turn, author NO durable user row.

    Your words become durable when the runner ships the transcript record they
    became — the same line the agent actually read — rather than a second copy
    written here at a different index. Until then they live in `Turn.prompt` and
    the client's optimistic echo, so a send that waits for an offline runner shows
    locally and becomes durable the moment the turn is executed.

    The returned Message is transient (never saved): MessageOut serializes it for
    the REST response and the WS handler broadcasts str(pk) as user_message_id, so
    both send contracts hold. A synthetic pk keeps those ids unique per send."""
    content = {"text": text}
    if client_id:
        content["client_id"] = client_id
    message = Message(
        session=session, turn_index=transcript_ingest._next_index(session), role=Message.USER,
        plaintext=text, content=content,
    )
    message.pk = f"transient:{uuid.uuid4().hex}"
    message.created_at = timezone.now()
    # Without a durable row, _next_index no longer advances between sends, so the
    # old index fallback would collapse DISTINCT no-nonce sends onto one turn —
    # fall back to a fresh nonce instead (same dedupe strength as before: only a
    # real client_id makes a retry idempotent).
    #
    # message=None: this path writes no durable user row, so the attachments'
    # sent_at stamp is the only thing stopping them riding along on every later send.
    with transaction.atomic():
        # A sensitive conversation's rows will be sealed to its key as the transcript
        # lands, so a send nobody could read back is refused before its turn exists —
        # and after, when the turn is what made it sensitive (rolled back with it).
        if sensitivity.seals(session):
            conversation_keys.require_key(session, user=user, contact=_contact_of(initiator))
        turn = _enqueue_chat_turn(
            session=session, message=None, text=text, user=user, client_id=client_id,
            fallback_key=uuid.uuid4().hex, placement=placement, origin=origin,
            initiator=initiator, origin_ref=origin_ref, capability=capability, parent=parent,
        )
        if sensitivity.seals(session, turn):
            conversation_keys.require_key(session, user=user, contact=_contact_of(initiator))
    return message, turn


def maybe_execute_inline(turn: Turn | None) -> None:
    """The chat send's executor hop. In dev/test (CHAT_STUB_EXECUTOR=True) run the
    stub inline so the turn completes with no runner. In production (False) leave it
    QUEUED for a session-capable cloud runner to claim + run real claude — the same
    ledger + Message projection either way. The one seam between stub and cloud.

    Guarded on QUEUED + IntegrityError so a truly-concurrent same-session send (the
    one_executing_turn_per_session race) never 500s the already-committed message."""
    if not getattr(settings, "CHAT_STUB_EXECUTOR", True):
        return
    if turn is None or turn.status != Turn.QUEUED:
        return
    from .executor import execute_turn_stub

    try:
        execute_turn_stub(turn)
    except IntegrityError:
        pass

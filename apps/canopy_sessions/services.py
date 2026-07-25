"""Chat services — create sessions, send messages (which enqueue a Turn), and
project the TurnEvent ledger into Message rows.

The write path is small: send_message writes the user Message + enqueues a session
Turn; the projection (driven by harness's turn_events_appended signal) materializes
the assistant/tool stream into Message rows. Because one_executing_turn_per_session
serializes a conversation, turn_index assignment never races within a session.
"""
from __future__ import annotations

import datetime as _dt
import uuid

from django.conf import settings
from django.db import IntegrityError, transaction
from django.db.models import Max
from django.utils import timezone

from apps.harness import services as harness_services
from apps.harness.models import Turn

from . import attach
from .models import Message, Session

# Ledger kinds we surface as transcript rows, and the Message role each maps to.
_ROLE_FOR_KIND = {
    "assistant": Message.ASSISTANT,
    "tool_start": Message.TOOL_USE,
    "tool_use": Message.TOOL_USE,
    "tool_end": Message.TOOL_RESULT,
    "tool_result": Message.TOOL_RESULT,
}

# --- Tail-first loading contract (Plan 2) ---------------------------------
# The server never ships a full transcript by default. SESSION_TAIL_DEFAULT is
# the single home for the tail size, shared by the REST handler and the WS
# snapshot so the two can't drift; SCROLLBACK_PAGE_DEFAULT is the "Load earlier"
# page size (aligned with apps/realtime's cursor-paging conventions).
SESSION_TAIL_DEFAULT = 20
SCROLLBACK_PAGE_DEFAULT = 50


def tail_messages(session: Session, limit: int | None = None):
    """The last `limit` messages, chronological, plus a backward cursor.

    Returns (messages, has_more_before, oldest_loaded_turn_index). This is what
    a client gets by default — enough to continue, never the whole history.
    """
    limit = SESSION_TAIL_DEFAULT if limit is None else limit
    newest_first = list(session.messages.order_by("-turn_index")[:limit])
    messages = list(reversed(newest_first))
    if not messages:
        return [], False, None
    oldest = messages[0].turn_index
    has_more = session.messages.filter(turn_index__lt=oldest).exists()
    return messages, has_more, oldest


def messages_before(session: Session, before: int, limit: int | None = None):
    """The window of up to `limit` messages immediately older than `before`
    (exclusive), chronological, plus whether anything older still exists.

    Returns (messages, has_more_before). Drives the scroll-back endpoint.
    """
    limit = SCROLLBACK_PAGE_DEFAULT if limit is None else limit
    newest_first = list(
        session.messages.filter(turn_index__lt=before).order_by("-turn_index")[:limit]
    )
    messages = list(reversed(newest_first))
    if not messages:
        return [], False
    has_more = session.messages.filter(turn_index__lt=messages[0].turn_index).exists()
    return messages, has_more


def all_messages(session: Session):
    """Every message, chronological — the explicit "load full session" escape
    hatch. Returns (messages, has_more_before=False, oldest_turn_index)."""
    messages = list(session.messages.order_by("turn_index"))
    if not messages:
        return [], False, None
    return messages, False, messages[0].turn_index


# A binding is "running" when its runner is live and it was interacted with very
# recently — the same signal OpenSessions derived client-side from the transcript
# tail's freshness, now computed once server-side.
RUNNING_WINDOW = _dt.timedelta(seconds=120)


def is_session_running(binding) -> bool:
    """True when a live runner is actively working this session right now."""
    from apps.harness.models import Runner  # framework->framework; lazy to avoid import cycle

    if binding is None or binding.runner_id is None:
        return False
    if binding.runner.live_status != Runner.ONLINE:
        return False
    ts = binding.last_interacted_at
    return bool(ts and (timezone.now() - ts) <= RUNNING_WINDOW)


_BACKFILL_ROLES = {Message.USER, Message.ASSISTANT, Message.TOOL_USE, Message.TOOL_RESULT, Message.SYSTEM}


def request_backfill(session) -> str:
    """The client asked for full history. 'ready' if already server-full; 'requested'
    if a live runner is bound (signal it); 'unavailable' otherwise (tail still shows)."""
    from apps.canopy_sessions.models import RunnerBinding
    from apps.harness.models import Runner

    if session.messages.exists():
        return "ready"
    binding = RunnerBinding.objects.select_related("runner").filter(session=session).first()
    # A runner only has to be REACHABLE to ship a transcript — not ready to run
    # turns. `live_status` returns the self-reported status ONLY while the
    # heartbeat is fresh (a quiet runner is demoted to STALE/DISCONNECTED), so
    # ONLINE and DEGRADED are exactly the "still reporting" states.
    # Gating on ONLINE alone made backfill impossible whenever emdash's CDP port
    # was down: the runner marks itself DEGRADED and stops CLAIMING, but its poll
    # loop keeps running and `_drain_backfills` reads the transcript FILE, which
    # never needed CDP. Found on prod — a degraded runner answered "unavailable"
    # for history it was perfectly able to ship.
    reachable = {Runner.ONLINE, Runner.DEGRADED}
    if binding is None or binding.runner_id is None or binding.runner.live_status not in reachable:
        return "unavailable"
    if not binding.backfill_requested:
        binding.backfill_requested = True
        binding.save(update_fields=["backfill_requested", "updated_at"])
    from apps.realtime import groups
    groups.publish(groups.runner_group(binding.runner_id), {
        "type": "runner.stream",  # reuse the control frame; desired=None marks a backfill ask
        "session_id": str(session.id), "session_key": binding.session_key, "desired": None,
    })
    return "requested"


def write_backfill(session, messages) -> int:
    """Write a runner's shipped transcript as Message rows — ONCE. No-op if the
    session already has rows (server-full). messages: [{"role","text"}] chronological."""
    with transaction.atomic():
        locked = Session.objects.select_for_update().get(pk=session.pk)
        if Message.objects.filter(session=locked).exists():
            return 0
        index = _next_index(locked)
        written = 0
        for msg in messages:
            role = msg.get("role")
            if role not in _BACKFILL_ROLES:
                continue
            Message.objects.create(
                session=locked, turn_index=index, role=role,
                content={"text": msg.get("text", ""), "backfill": True},
                plaintext=str(msg.get("text", "")),
            )
            index += 1
            written += 1
    return written


def _set_stream_desired(session, desired: bool) -> bool:
    """Flip the bound binding's stream_desired and, on a real change, signal the
    bound runner over its control channel. Returns the resulting desired state
    (False when the session has no binding to stream)."""
    from apps.canopy_sessions.models import RunnerBinding

    binding = RunnerBinding.objects.filter(session=session).first()
    if binding is None:
        return False
    if binding.stream_desired != desired:
        binding.stream_desired = desired
        binding.save(update_fields=["stream_desired", "updated_at"])
    if binding.runner_id:
        from apps.realtime import groups
        groups.publish(groups.runner_group(binding.runner_id), {
            "type": "runner.stream",
            "session_id": str(session.id),
            "session_key": binding.session_key,
            "desired": desired,
        })
    return desired


def attach_session(session) -> bool:
    """A viewer attached. On the 0->1 edge, mark streaming desired + signal the runner."""
    n = attach.attach(session.id)
    if n == 1:
        return _set_stream_desired(session, True)
    from apps.canopy_sessions.models import RunnerBinding
    b = RunnerBinding.objects.filter(session=session).first()
    return bool(b and b.stream_desired)


def detach_session(session) -> bool:
    """A viewer detached. On the 1->0 edge, stop streaming + signal the runner."""
    n = attach.detach(session.id)
    if n == 0:
        return _set_stream_desired(session, False)
    from apps.canopy_sessions.models import RunnerBinding
    b = RunnerBinding.objects.filter(session=session).first()
    return bool(b and b.stream_desired)


def create_session(*, workspace, created_by, agent=None, project: str = "", title: str = "", metadata: dict | None = None) -> Session:
    # The creator is the owner (SP3 multiplayer). Atomic so a session never exists
    # without its owner participant. Local imports avoid a cycle.
    from .models import SessionParticipant
    from .participants import ensure_participant

    with transaction.atomic():
        session = Session.objects.create(
            workspace=workspace, agent=agent, project=project, created_by=created_by,
            title=title, metadata=metadata or {},
        )
        ensure_participant(session, created_by, SessionParticipant.OWNER)
    return session


def _next_index(session: Session) -> int:
    current = Message.objects.filter(session=session).aggregate(m=Max("turn_index"))["m"]
    return 0 if current is None else current + 1


def _placeable_runner(session: Session, runner_id):
    """A runner may be a placement target only if it could actually CLAIM this
    session's turns — its pairer belongs to the session's workspace (mirrors
    claim_next_turn's tenant derivation from paired_by; a foreign or orphaned
    runner would leave the pinned turn permanently unclaimable) AND it is
    session-capable (capabilities.sessions — the runner-side truth for who
    may execute a chat turn; a pin can't override that). Invisible ids
    resolve to None so callers 422 exactly like a nonexistent id (no oracle);
    a malformed id (not a UUID) resolves to None the same way rather than
    raising django's ValidationError out of the ORM lookup."""
    from apps.harness.models import Runner
    from apps.workspaces import services as wsvc

    if not runner_id:
        return None
    try:
        uuid.UUID(str(runner_id))
    except (ValueError, AttributeError, TypeError):
        return None
    runner = (
        Runner.objects.filter(id=runner_id, paired_by__isnull=False)
        .exclude(status=Runner.RETIRED)
        .first()
    )
    if runner is None:
        return None
    if not runner.session_capable():
        return None
    if not wsvc.is_member(runner.paired_by, session.workspace_id):
        return None
    return runner


def _resolve_placement(session: Session, placement: str | None):
    """Directed-placement pin for a NEW turn about to be enqueued. `placement`
    wins when given explicitly; otherwise an unbound session's stashed
    `requested_runner_id` (set at directed-new-chat creation) pins the first
    turn. A live binding needs no pin here — claim-time stickiness already
    routes the turn to the binding holder (see claim_next_turn's session leg).

    Returns a Runner|None. Raises ValueError for an explicit but unresolvable
    placement (unknown/retired/foreign-tenant runner) — the caller surfaces
    that as a 422."""
    if placement == "wait":
        binding = getattr(session, "runner_binding", None)
        return binding.runner if binding and binding.runner_id else None
    if placement:
        pinned = _placeable_runner(session, placement)
        if pinned is None:
            raise ValueError("unknown runner for placement")
        return pinned
    if not getattr(session, "runner_binding", None):
        rid = (session.metadata or {}).get("requested_runner_id")
        if rid:
            return _placeable_runner(session, rid)
    return None


def send_message(
    *, session: Session, text: str, user, client_id: str = "", placement: str | None = None
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
    """
    with transaction.atomic():
        Session.objects.select_for_update().get(pk=session.pk)
        if client_id:
            existing = Message.objects.filter(
                session=session, role=Message.USER, content__client_id=client_id
            ).first()
            if existing is not None:
                key = f"chat:{session.id.hex}:{client_id}"
                turn = Turn.objects.filter(idempotency_key=key).first()
                return existing, turn
        index = _next_index(session)
        content = {"text": text}
        if client_id:
            content["client_id"] = client_id
        message = Message.objects.create(
            session=session, turn_index=index, role=Message.USER, plaintext=text, content=content,
        )
        pinned = _resolve_placement(session, placement)
        turn, _created = harness_services.enqueue_turn(
            session=session,
            origin=Turn.ORIGIN_API,
            idempotency_key=f"chat:{session.id.hex}:{client_id or index}",
            prompt=text,
            # Continuity: every send in a chat reuses ONE emdash session (the runner's
            # _thread_key reads this), so a conversation is one durable thread rather
            # than a fresh session per message. chat_session_id tells a session-capable
            # runner to BRIDGE the emdash response back into the ledger (vs the normal
            # fire-and-continue), so the website streams the reply.
            origin_ref={"thread_key": str(session.id), "chat_session_id": str(session.id)},
            pinned_runner=pinned,
        )
    # RC4 — multiplayer interjection: if a turn is ALREADY running for this session,
    # the human's message is an interjection. Push it down to the runner executing
    # that turn (over its control channel) so the live agent sees it, on top of the
    # new turn that queues behind it. Post-commit + null-safe (a realtime hiccup
    # never breaks the send).
    _maybe_interject(session, message)
    return message, turn


def place_queued_turn(*, session: Session, placement: str) -> Turn:
    """Re-pin a session's oldest QUEUED turn — the chat banner's after-the-fact
    directed-placement decision (vs `_resolve_placement`, which only applies to
    a turn being newly enqueued). `placement` is "wait" (pin to the session's
    currently bound runner) or a runner UUID string.

    Raises LookupError if there is no queued turn to place (-> 404), or
    ValueError for an unresolvable placement (-> 422): "wait" with no bound
    runner, or an unknown/retired runner id.
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
        runner = _placeable_runner(session, placement)
        if runner is None:
            raise ValueError("unknown runner")
        turn.pinned_runner = runner
    turn.save(update_fields=["pinned_runner"])
    return turn


def _maybe_interject(session: Session, message: Message) -> None:
    from apps.realtime import groups

    running = (
        Turn.objects.filter(
            chat_session=session,
            status__in=[Turn.CLAIMED, Turn.RUNNING, Turn.NEEDS_HUMAN],
            claimed_by__isnull=False,
        )
        .order_by("-created_at")
        .first()
    )
    if running is None:
        return
    groups.publish(groups.runner_group(running.claimed_by_id), {
        "type": "runner.interject",
        "turn_id": str(running.id),
        "session_id": str(session.id),
        "message": message.plaintext,
    })


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


def project_events(turn: Turn, rows) -> int:
    """Materialize a turn's newly-appended assistant/tool events into Message rows.
    Idempotent per source ledger seq, so a re-delivered signal never doubles a row."""
    if not turn.chat_session_id:
        return 0
    created = 0
    with transaction.atomic():
        session = Session.objects.select_for_update().get(pk=turn.chat_session_id)
        index = _next_index(session)
        for row in rows:
            role = _ROLE_FOR_KIND.get(row.kind)
            if role is None:
                continue  # status/heartbeat/error etc. are not transcript rows
            if Message.objects.filter(turn=turn, content__source_seq=row.seq).exists():
                continue
            payload = row.payload or {}
            Message.objects.create(
                session=session, turn=turn, turn_index=index, role=role,
                content={**payload, "source_seq": row.seq},
                plaintext=str(payload.get("text", "")),
            )
            index += 1
            created += 1
    return created

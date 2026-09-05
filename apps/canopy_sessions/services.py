"""Chat services — create sessions, send messages (which enqueue a Turn), and
project the TurnEvent ledger into Message rows.

The write path is small: send_message writes the user Message + enqueues a session
Turn; the projection (driven by harness's turn_events_appended signal) materializes
the assistant/tool stream into Message rows. Because one_executing_turn_per_session
serializes a conversation, turn_index assignment never races within a session.
"""
from __future__ import annotations

import datetime as _dt
import time
import uuid
from dataclasses import dataclass

from django.conf import settings
from django.db import IntegrityError, transaction
from django.db.models import Max
from django.utils import timezone

from apps.harness import services as harness_services
from apps.harness.models import Turn

from . import attach
from .models import Message, RunnerBinding, Session
from canopy_transcript import BLOCK_STRIDE  # noqa: F401  (the ordinal scheme's one definition)

from .transcript_noise import is_system_noise, scrub_nul

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
#
# 60, not 20, since tool calls became rows: measured over 19k rows of live
# transcripts (2026-07-26) ~72% of a session's rows are tool_use/tool_result, so
# a 20-row tail that used to open on 20 messages of conversation would now open
# on 5 or 6 — the default view would get THINNER as a direct result of adding
# detail to it. 3x holds the conversational density roughly where it was.
SESSION_TAIL_DEFAULT = 60
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


# FALLBACK signal only: a binding whose runner cannot say what its engine is doing
# is called "running" when it was interacted with this recently — the transcript-tail
# freshness OpenSessions once derived client-side. It infers work from writes, so it
# is wrong in both directions whenever a session is quiet for a reason: a turn inside
# a long tool call writes nothing for minutes and reads as FINISHED, and a turn that
# just stopped keeps reading as RUNNING until the window expires. Preferred, when the
# runner reports it, is the engine's own flag — see `is_session_running`.
RUNNING_WINDOW = _dt.timedelta(seconds=120)

# `RunnerBinding.agent_status` values that mean the engine is mid-turn. Anything else
# non-blank means it is not (emdash says "awaiting-input"); blank means "unknown".
WORKING_STATUSES = frozenset({"working"})

# Re-exported so callers keep one import surface; DEFINED in staleness.py, which the
# backfill migration also imports (see the module docstring there).
from .staleness import SESSION_LIVE_WINDOW, stale_cutoff, unseen_q  # noqa: E402,F401


def is_session_running(binding) -> bool:
    """True when a live runner is actively working this session right now.

    Asks the ENGINE first: emdash sets `agent_status` when it starts and stops driving
    a conversation, so a reported value is an observation of the session's actual state
    rather than an inference from writes. Its "not working" answer counts too — that is
    what retires the badge the moment a turn ends instead of two minutes later — EXCEPT
    where the runner has explicitly dissented (`agent_status_stale`). emdash reaches
    "working" only via Claude Code's UserPromptSubmit hook, so nothing short of a human
    typing can ever move the flag back: a turn that ended only to hand off to a
    background subagent leaves it pinned at "completed" while the session churns on.
    The dissent is the runner reporting that the session kept WRITING after the flag
    said it had stopped, which no genuinely finished turn does.

    Only when the runner cannot answer (blank: an older runner, a cloud runner with no
    emdash, a drifted schema) does this fall back to RUNNING_WINDOW, whose false
    negative is the whole reason the engine flag exists: a session sitting in a long
    tool call stops writing, so a list showing it as plain "12m ago" reads as finished
    while it is mid-turn.

    A runner that has gone offline is never running whatever it last reported — its
    answer describes a box that is no longer there to be believed.
    """
    from apps.harness.models import Runner  # framework->framework; lazy to avoid import cycle

    if binding is None or binding.runner_id is None:
        return False
    if binding.runner.live_status != Runner.ONLINE:
        return False
    if binding.agent_status:
        if binding.agent_status in WORKING_STATUSES:
            return True
        # The flag says stopped and the runner disagrees, having watched the session
        # keep writing AFTER it said so. Believe the writes: emdash's flag has no path
        # back to "working" that does not go through a human typing, so a turn that
        # ended only to hand off to a background subagent stays "completed" for the
        # rest of the session. See RunnerBinding.agent_status_stale.
        return bool(binding.agent_status_stale)
    ts = binding.last_interacted_at
    return bool(ts and (timezone.now() - ts) <= RUNNING_WINDOW)


_BACKFILL_ROLES = {Message.USER, Message.ASSISTANT, Message.TOOL_USE, Message.TOOL_RESULT, Message.SYSTEM}


def last_activity_at(session, binding):
    """When this session last DID something — not when its row was created.

    A runner-discovered session's row is created the moment the report sweep first
    sees it, so `created_at` is "when canopy first noticed you", identical for every
    session in that sweep. Rendering it made a long-dead repo and a live one both
    read "4h ago". The real signal is the binding's `last_interacted_at` (the runner
    reports it every tick); web sessions fall back to their newest message, then to
    creation. `_last_msg_at` is annotated by the callers so this stays N+1-free.
    """
    if binding is not None and binding.last_interacted_at:
        return binding.last_interacted_at
    return getattr(session, "_last_msg_at", None) or session.created_at


@dataclass(frozen=True)
class TailMessage:
    """A binding-tail entry shaped like a `Message` row.

    Quacks like the real model on purpose: the REST path serializes it with
    `MessageOut.from_orm` and the WebSocket path with `serializers.message_dto`,
    so BOTH transports render a local session's tail through their normal code
    with no special-casing. (ChatPage's transcript actually arrives over the WS
    snapshot — patching only REST left the panel blank.)
    """

    pk: str
    turn_index: int
    role: str
    plaintext: str
    content: dict
    created_at: object


def tail_as_messages(session, binding) -> list[TailMessage]:
    """A local runner session's reported tail, as Message-like rows.

    Local sessions hold NO `Message` rows until a backfill lands — the recent
    history lives on `RunnerBinding.tail` (what the retired OpenSessions used to
    render). Without this the converged ChatPanel opened blank on every discovered
    session even though the server had the last N messages in hand.

    turn_index is NEGATIVE (-n..-1): it orders the tail before any real row and can
    never collide with backfilled rows (which start at 0) or with a live stream's
    `seq:` ids, so a backfill or a live message layers on cleanly.

    The noise filter runs HERE too, not just on the durable paths. This is the one
    path that renders rows the server never inspected — a local session shows this
    tail until its backfill lands, and the runner filters it in the producer, which
    is exactly the arrangement `transcript_noise` exists to warn against. A runner
    on a lagging checkout shipped a tail containing a skill body and canopy rendered
    it in the human's own bubble (found 2026-07-30: superpowers/brainstorming, on an
    ada session). Sharing the prefix list fixes new runners; filtering here is what
    fixes every runner already in the field.
    """
    if binding is None or not binding.tail:
        return []
    ts = binding.last_interacted_at or session.created_at
    n = len(binding.tail)
    rows = []
    for i, m in enumerate(binding.tail):
        if not isinstance(m, dict):
            continue
        role = m.get("role") or Message.ASSISTANT
        if role not in _BACKFILL_ROLES:
            role = Message.ASSISTANT
        text = m.get("text") or ""
        # Scoped to USER rows for the same reason persist_transcript_rows scopes
        # it: the rule is about records masquerading as human input, and
        # assistant text quoting a marker is still the agent talking.
        if role == Message.USER and is_system_noise(text):
            continue
        # Derived from the ORIGINAL position, never a running counter — a dropped
        # row leaves its slot empty rather than shifting its neighbours, so the
        # tail keeps ordering consistently against itself.
        idx = i - n
        rows.append(TailMessage(
            pk=f"tail:{idx}", turn_index=idx, role=role,
            plaintext=text, content={"text": text}, created_at=ts,
        ))
    return rows


def visible_transcript(session, *, full: bool = False):
    """THE answer to "what transcript rows should a client see?" — used by every
    transport, so REST and the WebSocket can never disagree.

    Both transports previously reimplemented this. When the binding-tail fallback
    was added to the REST detail endpoint only, `GET` correctly returned 8 rows
    while the panel — which reads the `session.state` WS snapshot — stayed blank.
    The shared SESSION_TAIL_DEFAULT constant wasn't enough: the POLICY has to be
    shared too. `tests/test_transcript_parity.py` asserts the two agree.

    Returns (rows, has_more_before, oldest_loaded_turn_index). Rows are `Message`
    instances or `TailMessage`s, which serialize identically on both paths.
    """
    rows, has_more, oldest = (all_messages if full else tail_messages)(session)
    if not rows:
        # No server-side rows yet (a local runner session before backfill) — show
        # the binding's rolling tail rather than an empty panel.
        rows = tail_as_messages(session, getattr(session, "runner_binding", None))
    return rows, has_more, oldest


def request_backfill(session) -> str:
    """The client asked for full history. 'requested' if a reachable runner is
    bound (signal it, and `backfill_pending` stays true until it ships the last
    chunk); 'unavailable' otherwise (the tail still shows).

    There is deliberately NO 'we already have it' short-circuit any more. It used
    to return `ready` when a row existed at turn_index 0 — a check that cannot
    fire in practice: under the composite ordinal scheme (`record * BLOCK_STRIDE
    + block`) index 0 means record 0 / block 0, and record 0 of a Claude
    transcript is a summary or a noise-filtered harness record, both of which are
    DROPPED rather than renumbered. Verified on labs (2026-07-31): after a
    complete backfill, session cf2d5089's oldest index was 448 and a second click
    still answered `requested`. A condition that is always false is not an
    optimization, it is a claim the code makes and never honours.

    Asking unconditionally is now cheap in the way that matters: the write is
    ordinal-keyed, so a re-ship of rows we already hold costs one existence probe
    and zero inserts. `ready` survives in the client's vocabulary
    (`backfillAction`) for old servers."""
    from apps.canopy_sessions.models import RunnerBinding

    binding = RunnerBinding.objects.select_related("runner").filter(session=session).first()
    # A runner only has to be REACHABLE to ship a transcript — not ready to run
    # turns (`Runner.is_reachable`). Gating on ONLINE alone made backfill
    # impossible whenever emdash's CDP port was down: the runner marks itself
    # DEGRADED and stops CLAIMING, but its poll loop keeps running and
    # `_drain_backfills` reads the transcript FILE, which never needed CDP.
    # Found on prod — a degraded runner answered "unavailable" for history it
    # was perfectly able to ship. A PAUSED runner drains backfills the same way,
    # every tick before its pause gate.
    if binding is None or binding.runner_id is None or not binding.runner.is_reachable:
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


def storage_content(content: dict, text: str) -> dict:
    """The row's `content` as STORED — the wire payload minus its `text` key.

    The wire and the row deliberately share one shape (the live frame's `block`,
    the backfill payload, and this column are the same dict), and that shape has
    to carry `text` because the client builds a message from the frame alone.
    Storage does not: `plaintext` is its own column, and nothing on the render
    path reads `content["text"]` — MessageItem and ToolCallPair both use
    `plaintext`.

    Keeping the copy cost 36% of all stored transcript bytes (measured over
    20,585 rows of live transcripts, 2026-07-26: 9.4MB of 26.2MB), mostly tool
    result bodies duplicated verbatim. Only the redundant key is dropped: a
    `text` that somehow DIFFERS from plaintext is kept, and every other key
    (`id`, `name`, `input`, `tool_use_id`, `client_id`) is untouched.
    """
    if content.get("text") != text:
        return content
    return {k: v for k, v in content.items() if k != "text"}


# The transcript-ordinal scheme this build writes. Bumped whenever the mapping
# from a transcript record to a `turn_index` changes; see Session.ordinal_scheme.
ORDINAL_SCHEME = 1


def _ensure_current_ordinal_scheme(locked_session) -> int:
    """Drop rows written under a superseded ordinal scheme, so the incoming ones
    can't interleave with them.

    Two schemes in one session is not a cosmetic problem: `turn_index` is the
    sort order AND the paging cursor, so an old row at 500 and a new row for the
    same record at 32,000 would render the conversation shuffled, and
    `get_or_create` would never notice they are the same record.

    This is `reset` — the existing first-class action — fired automatically on
    the first write instead of waiting for someone to run it. Derived rows only;
    Turns and their ledger are never touched. Returns rows deleted.
    """
    if locked_session.ordinal_scheme == ORDINAL_SCHEME:
        return 0
    deleted, _ = Message.objects.filter(session=locked_session).delete()
    locked_session.ordinal_scheme = ORDINAL_SCHEME
    locked_session.save(update_fields=["ordinal_scheme", "updated_at"])
    return deleted


def ensure_transcript_identity(session, transcript_id: str) -> int:
    """Drop derived rows that came from a DIFFERENT transcript than the one now
    being shipped, so two conversations can't share one session's ordinals.

    The sibling of `_ensure_current_ordinal_scheme`, for the other way a session's
    `turn_index` space can be invalidated. A binding is keyed on the emdash task
    NAME, and names get reused — close "bednet", open another "bednet", and the
    binding is re-pointed at a new conversation with the old one's rows still
    attached. Because `turn_index` is a PER-FILE ordinal, that is not merely
    untidy: the first_index/last_index markers derived from the old file are
    nonsense against the new one, and a shorter successor sits entirely below the
    old high-water mark and is suppressed forever (issue #615).

    A change of transcript is therefore treated exactly like a change of ordinal
    scheme — drop and re-derive. That is safe because the runner ships the WHOLE
    history whenever its transcript id disagrees with the descriptor's (see
    `canopy_runner.streams`), so the rows are replaced, not lost, and the
    transcript on disk remains the source either way.

    Blank `transcript_id` (an old runner) is a no-op: it carries no claim about
    provenance, and dropping rows on no evidence would wipe a healthy session.
    Returns rows deleted.
    """
    if not transcript_id:
        return 0
    with transaction.atomic():
        # The BINDING is the only row that needs locking — it holds the flag that
        # makes this idempotent, so serializing on it is what stops two concurrent
        # ships both dropping. Deliberately NOT also locking the Session:
        # `harness.replace_reported_sessions` takes its locks binding-first and
        # then writes the session row, so grabbing them in the other order here
        # would make the two a deadlock pair — every ~10s report against every
        # ship. `persist_transcript_rows` still takes its own Session lock
        # afterwards, in its own transaction, exactly as before.
        binding = (
            RunnerBinding.objects.select_for_update().filter(session=session).first()
        )
        if binding is None or binding.transcript_id == transcript_id:
            return 0
        # First sighting (blank) still drops: a session that predates this field
        # is exactly the state issue #615 describes — rows of unknown provenance,
        # possibly a previous task's — and the shipper is sending the full history
        # for precisely that reason. Rebuilding once is cheap and self-healing.
        deleted, _ = Message.objects.filter(session=session).delete()
        binding.transcript_id = transcript_id
        binding.save(update_fields=["transcript_id", "updated_at"])
        return deleted


def persist_transcript_rows(session, rows) -> int:
    """THE durable write path for a runner session's transcript. rows:
    [{"index","role","text"[,"content"]}] chronological.

    `index` is the transcript ordinal (`record * BLOCK_STRIDE + block` — see
    `canopy_transcript.compose_index`, imported above so this scheme has exactly
    one definition) — because the stream (forward) and
    backfill (older) both key on it, they produce the SAME rows by identity, so
    every re-ship (retry, overlap, catch-up) is a no-op.
    index < 0 (an old runner) falls back to sequential server-side assignment.
    Returns rows actually created.

    BULK, deliberately. This was one `get_or_create` per row — four round trips
    each (SELECT, SAVEPOINT, INSERT, RELEASE), measured at 805 queries for 200
    rows. On labs a 846-row backfill took ~14.6s end to end while the runner's
    whole share (reading and parsing a 6.5 MB transcript) was 29 ms; the rest was
    sequential round trips to RDS. Every durable path funnels through here — live
    stream, backfill, reset — so the cost was paid on all of them, and it scaled
    with session length, i.e. it was worst exactly where history matters most.
    Now: one existence probe plus batched inserts, regardless of row count."""
    with transaction.atomic():
        locked = Session.objects.select_for_update().get(pk=session.pk)
        # `is not None`, never a truthiness test: index 0 is a real ordinal (the
        # transcript's first record) and `x or -1` would read it as "no ordinal".
        if any(r.get("index") is not None and int(r["index"]) >= 0 for r in rows):
            _ensure_current_ordinal_scheme(locked)
        next_index = None
        prepared: list[tuple[int, str, str, dict]] = []
        claimed: set[int] = set()
        for row in rows:
            role = row.get("role")
            if role not in _BACKFILL_ROLES:
                continue
            text = str(row.get("text", ""))
            # A Claude transcript records harness output (task notifications,
            # system reminders, local command stdout) as `type: "user"`, so
            # without this the machine's event stream renders on the HUMAN's side
            # of the chat. Scoped to USER rows: the rule is about records
            # masquerading as human input, and assistant text that happens to
            # quote a marker is still the agent talking.
            #
            # The row is DROPPED, never renumbered — turn_index is the transcript
            # ordinal that the live stream, catch-up and backfill all key on, so
            # closing the gap would make one record arrive under two indices.
            if role == Message.USER and is_system_noise(text):
                continue
            index = row.get("index")
            index = -1 if index is None else int(index)
            if index < 0:
                if next_index is None:
                    next_index = _next_index(locked)
                index, next_index = next_index, next_index + 1
            # First occurrence wins, matching what `get_or_create` did implicitly:
            # a repeat within ONE payload used to find the row its predecessor had
            # just written. A bulk insert has no such ordering, and the pair would
            # violate the unique constraint, so the dedupe has to be explicit.
            if index in claimed:
                continue
            claimed.add(index)
            content = row.get("content")
            if not isinstance(content, dict):
                content = {}
            # Postgres rejects NUL in text/jsonb, and the batch is ONE
            # transaction — an unscrubbed byte from a binary tool result 500s
            # every other row with it. See transcript_noise.scrub_nul.
            text = scrub_nul(text)
            content = storage_content(scrub_nul(content), text)
            prepared.append((index, role, text, content))
        if not prepared:
            return 0
        held = set(
            Message.objects.filter(
                session=locked, turn_index__in=[p[0] for p in prepared]
            ).values_list("turn_index", flat=True)
        )
        fresh = [
            Message(session=locked, turn_index=i, role=r, plaintext=t, content=c)
            for (i, r, t, c) in prepared
            if i not in held
        ]
        if not fresh:
            return 0
        # `ignore_conflicts` is belt-and-braces on top of the row lock above (which
        # already serializes writers for THIS session), so a racing writer costs a
        # skipped row rather than a failed batch.
        Message.objects.bulk_create(fresh, batch_size=500, ignore_conflicts=True)
        return len(fresh)


def write_backfill(session, messages) -> int:
    """Write a runner's shipped full transcript as Message rows. Ordinal-keyed
    payloads (a current runner) upsert-fill: they add the older rows the live
    stream never saw and skip anything already persisted. A legacy payload (no
    ordinals) keeps the old write-once contract — sequential, and only into an
    empty session. messages: [{"role","text"[,"index"]}] chronological."""
    ordinal = any(int(m.get("index", -1)) >= 0 for m in messages)
    if not ordinal and Message.objects.filter(session=session).exists():
        return 0
    return persist_transcript_rows(session, messages)


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

    meta = dict(metadata or {})
    # A real runner will drive this session in emdash, so its transcript is the
    # record — stamped at birth, never inferred later, so a session can't change
    # its mind about where its history lives (see `transcript_sourced`). Under the
    # dev stub there is no emdash session and no transcript, so the ledger stays
    # the source.
    if not getattr(settings, "CHAT_STUB_EXECUTOR", True):
        meta.setdefault(TRANSCRIPT_SOURCED, True)
    with transaction.atomic():
        session = Session.objects.create(
            workspace=workspace, agent=agent, project=project, created_by=created_by,
            title=title, metadata=meta,
        )
        ensure_participant(session, created_by, SessionParticipant.OWNER)
    return session


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
    session.metadata = {**(session.metadata or {}), TRANSCRIPT_SOURCED: True}
    session.save(update_fields=["metadata", "updated_at"])
    request_backfill(session)
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



def claim_pending_attachments(session, message=None) -> list[dict]:
    """Mark this session's un-sent attachments as sent, and describe them for the
    runner.

    Swept off the SESSION rather than passed by id, so the WebSocket `chat.send`
    frame needs no new field and REST and WS behave identically. It also matches
    the draft model: the draft is co-edited and shared, so anything attached to
    it belongs to the send whoever presses the button.

    `message` is None for a runner-origin session, which writes no user Message
    row — hence the sent_at stamp, without which those rows would ride along on
    every later send too.
    """
    from .models import Attachment

    pending = list(Attachment.objects.filter(session=session, sent_at__isnull=True))
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


def send_message(
    *, session: Session, text: str, user, client_id: str = "", placement: str | None = None,
    origin: str | None = None,
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
    if transcript_sourced(session):
        return _send_transcript_sourced_message(
            session=session, text=text, user=user, client_id=client_id,
            placement=placement, origin=origin,
        )
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
        # Continuity: every send in a chat reuses ONE emdash session (the runner's
        # _thread_key reads this), so a conversation is one durable thread rather
        # than a fresh session per message. chat_session_id tells a session-capable
        # runner to BRIDGE the emdash response back into the ledger (vs the normal
        # fire-and-continue), so the website streams the reply.
        #
        # A RUNNER-DISCOVERED session already has a binding keyed `emdash:<task>` (the
        # report sweep wrote it). Sending str(session.id) there matched nothing, so
        # resolve_session answered new_thread and the runner SPAWNED A FRESH emdash
        # session instead of typing into the live one you were looking at. Prefer the
        # binding's existing thread_key; web sessions (no binding yet) keep the
        # session id, which is what record_session then stores.
        binding = getattr(session, "runner_binding", None)
        thread_key = binding.thread_key if (binding and binding.thread_key) else str(session.id)
        pinned = _resolve_placement(session, placement)
        origin_ref = {"thread_key": thread_key, "chat_session_id": str(session.id)}
        attachments = claim_pending_attachments(session, message)
        if attachments:
            origin_ref["attachments"] = attachments
        turn, _created = harness_services.enqueue_turn(
            session=session,
            origin=origin,
            idempotency_key=f"chat:{session.id.hex}:{client_id or index}",
            prompt=text,
            origin_ref=origin_ref,
            # WHO sent it. Not decoration: this is the actor half of the routing key
            # (spec 2026-09-05), and it is the ONLY place an `ace_web` or
            # `canopy_web_chat` turn can get one — neither carries the
            # `origin_ref["from"]` an email turn is routed by. Left unset, every actor
            # rule on those sources silently matches nothing. `enqueue_turn` ignores an
            # unauthenticated user, so this is safe to pass unconditionally.
            enqueued_by=user,
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


def _send_transcript_sourced_message(
    *, session: Session, text: str, user=None, client_id: str = "",
    placement: str | None = None, origin: str = Turn.ORIGIN_CANOPY_WEB_CHAT,
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
        session=session, turn_index=_next_index(session), role=Message.USER,
        plaintext=text, content=content,
    )
    message.pk = f"transient:{uuid.uuid4().hex}"
    message.created_at = timezone.now()
    binding = getattr(session, "runner_binding", None)
    thread_key = binding.thread_key if (binding and binding.thread_key) else str(session.id)
    # Without a durable row, _next_index no longer advances between sends, so the
    # old index fallback would collapse DISTINCT no-nonce sends onto one turn —
    # fall back to a fresh nonce instead (same dedupe strength as before: only a
    # real client_id makes a retry idempotent).
    pinned = _resolve_placement(session, placement)
    origin_ref = {"thread_key": thread_key, "chat_session_id": str(session.id)}
    # message=None: this path writes no durable user row, so the sent_at stamp is
    # the only thing stopping these attachments riding along on every later send.
    attachments = claim_pending_attachments(session, None)
    if attachments:
        origin_ref["attachments"] = attachments
    turn, _created = harness_services.enqueue_turn(
        session=session,
        origin=origin,
        idempotency_key=f"chat:{session.id.hex}:{client_id or uuid.uuid4().hex}",
        prompt=text,
        origin_ref=origin_ref,
        # WHO sent it. Not decoration: this is the actor half of the routing key
        # (spec 2026-09-05), and it is the ONLY place an `ace_web` or
        # `canopy_web_chat` turn can get one — neither carries the
        # `origin_ref["from"]` an email turn is routed by. Left unset, every actor
        # rule on those sources silently matches nothing. `enqueue_turn` ignores an
        # unauthenticated user, so this is safe to pass unconditionally.
        enqueued_by=user,
        pinned_runner=pinned,
    )
    _maybe_interject(session, message)
    return message, turn


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
    Idempotent per source ledger seq, so a re-delivered signal never doubles a row.

    Runner sessions are excluded: their durable rows come from the transcript
    (ordinal-keyed, via persist_transcript_rows) — the bridged reply lands in the
    ledger too, and projecting it as well would persist it twice in a second
    index space. The ledger frames still stream to the live client unchanged."""
    if not turn.chat_session_id:
        return 0
    if transcript_sourced(turn.chat_session):
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


def answer_menu(*, session: Session, option: int | None,
                selections: list[list[int]] | None = None,
                texts: list[str | None] | None = None) -> str:
    """Answer the dialog an agent is blocked on, from the web.

    `selections` is the whole answer: one list of chosen option numbers per
    declared question, in declaration order. It is what a multi-select or a
    multi-question ask needs, because there a number key toggles a checkbox and
    the dialog waits on an explicit Submit — a single `option` cannot express
    "Red and Blue", and cannot reach the tab holding the second question at all.

    `option` is still sent alongside it, set to the first pick, and is the ONLY
    field a runner older than this understands. That is deliberate: such a runner
    keeps doing exactly what it does today rather than seeing an empty option and
    pressing Escape, which would cancel the dialog outright.

    Returns "sent" | "unavailable" | "unbound", mirroring `request_backfill`'s
    refusal shape rather than raising: a menu can go stale between the phone
    rendering it and a thumb reaching it, and that is ordinary, not an error.

    `option=None` means refuse, which the runner sends as Escape. Escape is the
    one answer that is safe when the dialog is not what we think it is — a NUMBER
    typed at a session that is no longer showing a menu lands in its prompt.

    The keystroke itself is the runner's job: the server knows nothing about
    terminals, and emdash (not canopy) owns the session.
    """
    binding = getattr(session, "runner_binding", None)
    if binding is None or binding.runner_id is None or not binding.session_key:
        return "unbound"
    # Reachable, not available: pause stops STARTING work, never finishing it,
    # and a blocked agent is unfinished work already running. The answer rides
    # the wake-listener thread, which the pause gate never touches — a PAUSED
    # runner (fresh heartbeat by construction) presses the key just fine, and it
    # is the runner whose session report delivered this very menu.
    if not binding.runner.is_reachable:
        return "unavailable"
    # Record BEFORE publishing. The frame is the doorbell; this is the record the
    # runner drains on its poll tick. Publishing alone is how an answer gets lost
    # in silence when the control channel is down — see RunnerBinding.pending_answer.
    answer_id = str(uuid.uuid4())
    binding.pending_answer = {"id": answer_id, "option": option,
                              "selections": selections, "texts": texts,
                              "at": time.time()}
    binding.save(update_fields=["pending_answer"])

    from apps.realtime import groups
    groups.publish(groups.runner_group(binding.runner_id), {
        "type": "runner.menu_answer",
        "session_id": str(session.id),
        "session_key": binding.session_key,
        "option": option,
        "selections": selections,
        "texts": texts,
        "answer_id": answer_id,
    })
    return "sent"


def interrupt_session(session: Session) -> str:
    """Stop whatever this session's agent is doing, by interrupting its TERMINAL.

    The turn-shaped stop (`cancel_session_turns` below) can only reach work a
    non-terminal Turn still owns, which in practice means chat. An agent, board or
    scheduled turn is fire-and-continue: `execute_turn` finishes it the moment the
    prompt is delivered (runner execute.py), so seconds later the agent is working
    hard on a turn that is already DONE, and a stop keyed on turns finds nothing to
    cancel and silently does nothing.

    But the work is not turn-shaped, it is SESSION-shaped, and canopy already knows
    the session: `harness.services.record_session` gives every agent/project/phone
    thread a durable Session plus a RunnerBinding carrying `session_key` — the very
    same binding `answer_menu` above uses to press a key into that terminal. So
    stopping is addressed exactly like answering: name the session, let the runner
    own the keystroke.

    Returns "sent" | "unavailable" | "unbound", mirroring `answer_menu`'s refusal
    shape rather than raising — a session can go idle between a thumb reaching the
    button and the frame landing, and that is ordinary, not an error.
    """
    binding = getattr(session, "runner_binding", None)
    if binding is None or binding.runner_id is None or not binding.session_key:
        return "unbound"
    # Same reasoning as answer_menu: pause stops STARTING work, never finishing it,
    # and an agent mid-turn is work already running. Reachable, not available.
    if not binding.runner.is_reachable:
        return "unavailable"

    from apps.realtime import groups
    groups.publish(groups.runner_group(binding.runner_id), {
        "type": "runner.session_interrupt",
        "session_id": str(session.id),
        "session_key": binding.session_key,
    })
    return "sent"


def cancel_session_turns(session: Session) -> bool:
    """Cancel every non-terminal turn on a session. Returns whether anything moved.

    ALL non-terminal turns, not just the newest: a mid-reply send queues a second
    turn behind the one still running, so both must be reached — the running one
    gets cancel_requested, the queued one is finished CANCELLED.

    Deliberately NOT `any(cancel_turn(t) for t in turns)`: any() short-circuits on
    the first truthy result and would skip every turn after it.
    """
    from apps.harness import services as harness_services  # framework->framework; lazy
    from apps.harness.models import Turn

    cancelled = False
    for turn in Turn.objects.filter(chat_session=session, status__in=list(Turn.NON_TERMINAL)):
        if harness_services.cancel_turn(turn) is not None:
            cancelled = True
    return cancelled


def _is_runner_reported(binding) -> bool:
    """Is a runner CURRENTLY reporting an emdash task for this session?

    The one question `close_session` branches on, observed rather than inferred.
    `Runner.kind` would answer "what program is this" — a different question, and
    already deprecated as a behavioural input. `live_seen_at` and `session_key`
    cannot answer it at all: `record_session` is called by BOTH runners and stamps
    both, with the cloud runner writing a Claude session id where the laptop writes
    an emdash task name. Hence `reported_at`, which only the report loop writes.

    Read against the same `stale_cutoff()` the session list uses, so "reported"
    and "live" can never drift into meaning different windows.
    """
    if binding is None or binding.runner_id is None or not binding.session_key:
        return False
    if binding.reported_at is None:
        return False
    return binding.reported_at >= stale_cutoff()


def close_session(*, session: Session) -> str:
    """End a session for good. Returns
    "closing" | "closed" | "unavailable" | "already_closed".

    Two branches on one question — see `_is_runner_reported`.

    REPORTED (a laptop's emdash task): cancel the turns, then relay a close and
    write NOTHING to the session. The emdash task is the truth for a local session,
    and `replace_reported_sessions` un-archives anything re-reported as open, so a
    status write here would be undone within ~10s anyway. The runner deletes the
    task and puts its name in the `archived:` closing signal on its next report;
    that is what retires the row.

    UNREPORTED (a cloud session, a web chat that never bound): nothing exists on a
    box. Cancel the turns so a queued one cannot wake it, archive, done — and it
    sticks, because nothing will ever report it back.

    A refusal is a returned reason, never a raise: a session can go stale between
    the phone rendering the list and a thumb reaching it, which is ordinary rather
    than a client error. `unavailable` deliberately does NOT queue — a close that
    sits until a box returns is indistinguishable from one that worked.
    """
    from apps.harness.models import Runner  # framework->framework; lazy, import cycle

    if session.status == Session.ARCHIVED:
        return "already_closed"

    binding = getattr(session, "runner_binding", None)  # reverse 1:1 -> None when absent
    if _is_runner_reported(binding):
        reachable = {Runner.ONLINE, Runner.DEGRADED}
        if binding.runner.live_status not in reachable:
            return "unavailable"
        # Cancel BEFORE relaying. Deleting the emdash task kills the process the
        # turn runs in, so a live turn would otherwise stay EXECUTING with nobody
        # left to finish it — held until the lease sweep, wedging the agent through
        # one_executing_turn_per_agent. Cancelling first also means the ledger
        # records a cancellation rather than a turn that merely stops emitting.
        cancel_session_turns(session)
        # Record BEFORE relaying, for the same reason `answer_menu` does: the frame
        # is the doorbell and this is what survives the channel being down. Without
        # it a lost frame leaves the emdash task open and the session active
        # forever — and `/close`'s own fallback ("the task's plain absence from the
        # following report retires it anyway") assumes the runner deleted the task,
        # which never happened. Verified 2026-08-01: the API answered
        # `{"ok":true,"closing":true}` and the runner logged nothing at all.
        binding.close_requested = True
        binding.save(update_fields=["close_requested"])
        from apps.realtime import groups

        groups.publish(groups.runner_group(binding.runner_id), {
            "type": "runner.close_session",
            "session_id": str(session.id),
            "session_key": binding.session_key,
        })
        return "closing"

    cancel_session_turns(session)
    session.status = Session.ARCHIVED
    session.save(update_fields=["status", "updated_at"])
    return "closed"

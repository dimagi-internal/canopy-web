"""Chat services — create sessions, send messages (which enqueue a Turn), and
project the TurnEvent ledger into Message rows.

The write path is small: send_message writes the user Message + enqueues a session
Turn; the projection (driven by harness's turn_events_appended signal) materializes
the assistant/tool stream into Message rows. Because one_executing_turn_per_session
serializes a conversation, turn_index assignment never races within a session.
"""
from __future__ import annotations

import datetime as _dt
import re
import time
import uuid
from dataclasses import dataclass

from django.conf import settings
from django.db import IntegrityError, transaction
from django.db.models import Max, OuterRef, Subquery
from django.utils import timezone

from apps.harness import services as harness_services
from apps.harness.models import Turn

from . import attach, authorship
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


#: What an embedded widget never receives: the agent's tool calls and their
#: results. A widget's visitor asked a question and wants the answer; which MCP
#: tools ran, with what arguments, is noise to them and often a page of JSON.
#: Not a host option — a widget cannot ask for them. The transcript keeps every
#: call, and canopy's own chat page shows them.
WIDGET_HIDDEN_ROLES = frozenset({Message.TOOL_USE, Message.TOOL_RESULT})


def for_widget(messages):
    """`messages` as an embedded widget may see them — without tool rows."""
    return [m for m in messages if getattr(m, "role", None) not in WIDGET_HIDDEN_ROLES]


#: How much of a conversation's first message a list shows to name it.
OPENING_CHARS = 140


def with_opening(sessions):
    """`sessions` annotated with `_opening`: the text of each one's first user
    message, in the same query — a list of fifty must not become fifty-one."""
    first = (
        Message.objects.filter(session=OuterRef("pk"), role=Message.USER)
        .order_by("turn_index")
        .values("plaintext")[:1]
    )
    return sessions.annotate(_opening=Subquery(first))


def opening_of(session) -> str:
    """What the person first asked, as a list names a conversation.

    A widget's session title is canopy's, not the visitor's, and reads as noise
    to them; the question they typed is the name they would recognise. Only the
    first paragraph: the widget appends the page's context block to the opening
    message after a blank line, and that is canopy talking, not the person.
    """
    text = (getattr(session, "_opening", None) or "").strip()
    text = " ".join(text.split("\n\n", 1)[0].split())
    return text if len(text) <= OPENING_CHARS else text[: OPENING_CHARS - 1].rstrip() + "…"


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
    author: dict | None = None


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
        author = None
        if role == Message.USER:
            # Same marker, same parse, as the durable path — a tail row is a
            # message like any other and must not show a person the marker
            # syntax while their conversation is still local-only.
            author, text, _turn = authorship.parse(text)
        # Derived from the ORIGINAL position, never a running counter — a dropped
        # row leaves its slot empty rather than shifting its neighbours, so the
        # tail keeps ordering consistently against itself.
        idx = i - n
        rows.append(TailMessage(
            pk=f"tail:{idx}", turn_index=idx, role=role,
            plaintext=text, content={"text": text}, created_at=ts, author=author,
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


def _ensure_current_ordinal_scheme(locked_session, offset: int = 0) -> int:
    """Drop rows written under a superseded ordinal scheme, so the incoming ones
    can't interleave with them.

    Two schemes in one session is not a cosmetic problem: `turn_index` is the
    sort order AND the paging cursor, so an old row at 500 and a new row for the
    same record at 32,000 would render the conversation shuffled, and
    `get_or_create` would never notice they are the same record.

    This is `reset` — the existing first-class action — fired automatically on
    the first write instead of waiting for someone to run it. Derived rows only;
    Turns and their ledger are never touched. Returns rows deleted.

    Scoped to the current epoch (`offset`), for the same reason
    `ensure_transcript_identity` is — and with a stronger justification, because
    the remedy this function relies on is not available across a transfer.
    "Drop and re-derive" is safe only while something can re-derive; the rows a
    transfer carried across were derived from a transcript on a box this server
    may never be able to reach again, so dropping them is final. The
    interleaving hazard does not reach across the boundary either: the offset
    guarantees every inherited row sorts below every new one whatever scheme
    composed their numbers, so epoch order dominates scheme and the render stays
    chronological. Two epochs in two schemes is therefore fine; two SCHEMES in
    ONE epoch is what this still prevents.
    """
    if locked_session.ordinal_scheme == ORDINAL_SCHEME:
        return 0
    deleted, _ = Message.objects.filter(
        session=locked_session, turn_index__gte=offset
    ).delete()
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

    Scoped to the CURRENT EPOCH — rows at or above `binding.index_offset`. Below
    it is history carried across a TRANSFER (`transfer_session`), which is the one
    case where a changed `transcript_id` does NOT mean "a different conversation's
    rows are attached": it is the same conversation on a new box, which
    necessarily opened a fresh claude session with a fresh id. Unscoped, this
    function deletes exactly the thing a transfer exists to preserve — measured
    2026-09-12 on session 169212e2 moving cloud-ec2-1 -> jj-mbp-cdp, which lost
    every one of its 60+ pre-transfer rows the moment the laptop shipped its
    first transcript. For a never-transferred session the offset is 0 and the
    scope is the whole session, i.e. exactly the previous behaviour.
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
        deleted, _ = Message.objects.filter(
            session=session, turn_index__gte=binding.index_offset
        ).delete()
        binding.transcript_id = transcript_id
        binding.save(update_fields=["transcript_id", "updated_at"])
        return deleted


def persist_transcript_rows(session, rows, *, attribute: bool = True) -> int:
    """THE durable write path for a runner session's transcript. rows:
    [{"index","role","text"[,"content"]}] chronological.

    `attribute=False` (used only by `write_backfill`) skips the text-match
    half of server-side attribution below — see that half's docstring for
    why a full-history ship cannot use it safely. The vouched-marker path is
    unaffected either way: it names an exact turn id, so it carries no
    ordering risk.

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
    Now: one existence probe plus batched inserts, regardless of row count.

    Ordinals are shifted by the binding's `index_offset` on the way in, so a
    transferred session's new box writes ABOVE the history it inherited instead of
    colliding with it. THE single funnel for that shift, deliberately: live
    stream, backfill and reset all come through here, so applying it anywhere
    upstream would mean applying it three times and forgetting it once. The
    identity property the docstring above rests on survives — a given
    (transcript ordinal, epoch) still maps to exactly one `turn_index`, so
    re-ships stay no-ops. Offset 0 (never transferred) is a no-op addition.

    **Server-side attribution (2026-09-27), the primary path now that canopy no
    longer marks a chat send's prompt at claim** (authorship.py). A USER row
    with no vouched marker is matched to the EARLIEST of this session's
    claimed, unlinked chat-send turns whose `prompt` equals the row's text
    with ALL whitespace ignored (`_squash` — the laptop runner delivers a
    prompt via CDP `keyboard.insertText`, which drops newlines, so a
    multi-line send comes back with none; `.strip()` alone still needed the
    newline to survive) — lazily loaded and consumed within the batch via
    `_MatchPool` — one query, only when a row actually needs it, so a batch
    of entirely vouched or entirely unmatched rows costs nothing extra). No
    match leaves `author=None`, exactly the right answer for a line typed
    straight into emdash. The vouched-marker path (m3) is checked first and
    wins when present — it is a stronger claim (the transcript named a turn,
    and the turn's own initiator agrees) than a text match ever is.

    **Only the LIVE stream path attributes; a backfill never does**
    (`attribute=False`, above). `_MatchPool` orders candidates by
    `Turn.created_at`, which has no relation to when a transcript ROW
    happened — a backfill ships a session's full history in one shot, and
    neither `canopy_transcript.conversational_messages`/`row_payload` nor
    `write_backfill`'s own `messages` shape carries a per-row timestamp on
    the wire today. Without one, an old backfilled "yes" from last week could
    link to a turn created TODAY, and today's real "yes" would then miss it —
    a live-shipped row has no such gap (it ships as it happens, so ordering
    by claim time is sound). Revisit if the wire ever carries a row
    timestamp: the tighter fix is bounding candidates to
    `claimed_at <= row_ts + 2min` and `created_at >= row_ts - 7d`, not
    disabling the match outright.

    **`held` (which rows are already persisted) is computed BEFORE any
    matching, never after.** It used to run once, on `prepared`'s indices,
    right before `bulk_create` — by which point every row, including one
    whose index already exists (a re-ship: reconnect, catch-up, backfill
    overlap — routine, not an error), had already called `pool.claim`. A
    re-shipped row is dropped at `bulk_create` and so was never going to
    consume a candidate — but it did, stealing the match a genuinely NEW row
    in the same batch with the same (squashed) text needed, which then got
    `author=None` or the wrong turn. Only an explicit ordinal (`index >= 0`)
    can already be held: a row with none always gets a FRESH one from
    `_next_index`, one past the session's high-water mark, so it can never
    collide — the precomputed set below only needs to check those."""
    offset = _index_offset(session)
    with transaction.atomic():
        locked = Session.objects.select_for_update().get(pk=session.pk)
        # `is not None`, never a truthiness test: index 0 is a real ordinal (the
        # transcript's first record) and `x or -1` would read it as "no ordinal".
        if any(r.get("index") is not None and int(r["index"]) >= 0 for r in rows):
            _ensure_current_ordinal_scheme(locked, offset)
        held = set(
            Message.objects.filter(
                session=locked,
                turn_index__in=[
                    int(r["index"]) + offset for r in rows
                    if r.get("index") is not None and int(r["index"]) >= 0
                ],
            ).values_list("turn_index", flat=True)
        )
        next_index = None
        prepared: list[tuple[int, str, str, dict, dict | None, str | None]] = []
        claimed: set[int] = set()
        vouched = _vouched_markers(locked, rows)
        pool = _MatchPool(locked) if attribute else None
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
            else:
                # Only ordinal-keyed rows shift. The `index < 0` leg above already
                # assigns an ABSOLUTE index off the session's own high-water mark,
                # so offsetting it too would double-count.
                index += offset
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
            author = turn_hex = None
            if role == Message.USER:
                # The marker canopy prepended at claim (authorship.py). Stripped
                # here, the single funnel for live stream, backfill and reset, so
                # every path gets the same text and the same author.
                author, bare, turn_hex = authorship.parse(text)
                if author is not None and vouched.get(turn_hex) == _marker_identity(author):
                    text = bare
                    if isinstance(content.get("text"), str):
                        content = {**content, "text": authorship.parse(content["text"])[1]}
                else:
                    # Unmarked, or a marker nobody can vouch for: anyone who can
                    # type into the transcript can write the syntax, so a line
                    # naming a turn that is not this session's, or a person who
                    # did not send that turn, stays exactly what it was typed as.
                    author = turn_hex = None
                    # A row whose index is already `held` will be dropped at
                    # bulk_create below (it's a re-ship) — never spend a
                    # match candidate on it.
                    if pool is not None and index not in held:
                        matched = pool.claim(text)
                        if matched is not None:
                            matched_author = authorship.author_of(matched)
                            if matched_author is not None:
                                author, turn_hex = matched_author, matched.pk.hex
            # Postgres rejects NUL in text/jsonb, and the batch is ONE
            # transaction — an unscrubbed byte from a binary tool result 500s
            # every other row with it. See transcript_noise.scrub_nul.
            text = scrub_nul(text)
            content = storage_content(scrub_nul(content), text)
            prepared.append((index, role, text, content, author, turn_hex))
        if not prepared:
            return 0
        # `held` was computed above, before matching — reused here, not
        # re-queried.
        fresh = [
            Message(session=locked, turn_index=i, role=r, plaintext=t, content=c,
                    author=a, source_turn_id=uuid.UUID(h) if h else None)
            for (i, r, t, c, a, h) in prepared
            if i not in held
        ]
        if not fresh:
            return 0
        # `ignore_conflicts` is belt-and-braces on top of the row lock above (which
        # already serializes writers for THIS session), so a racing writer costs a
        # skipped row rather than a failed batch.
        Message.objects.bulk_create(fresh, batch_size=500, ignore_conflicts=True)
        return len(fresh)


def _marker_identity(author: dict) -> tuple[int | None, int | None]:
    return author.get("user_id"), author.get("contact_id")


def _vouched_markers(session, rows) -> dict[str, tuple[int | None, int | None]]:
    """turn hex -> (initiator_user_id, initiator_contact_id), for every turn a
    user row's marker names that really is one of THIS session's turns. One
    query for the whole batch, whatever its size.

    The live frame (`stream_map`) is deliberately not checked the same way — it
    has no cheap query, and the durable row written here is what every reload
    shows."""
    hexes = set()
    for row in rows:
        if row.get("role") != Message.USER:
            continue
        _author, _bare, turn_hex = authorship.parse(str(row.get("text", "")))
        if turn_hex:
            hexes.add(turn_hex)
    if not hexes:
        return {}
    found = Turn.objects.filter(
        chat_session=session, pk__in=[uuid.UUID(h) for h in hexes],
    ).values_list("pk", "initiator_user_id", "initiator_contact_id")
    return {pk.hex: (uid, cid) for pk, uid, cid in found}


def _squash(text: str) -> str:
    """Whitespace-blind comparison key. The laptop runner delivers a prompt via
    CDP `keyboard.insertText` into emdash's Claude Code TUI, which DROPS
    newlines — a two-line send ("line one\nline two") comes back in the
    transcript as "line oneline two", so a plain `.strip()` equality (which
    still requires the newline to survive) missed every multi-line message.
    Squashing ALL whitespace out of both sides makes the match blind to
    exactly the characters the delivery path is known to mangle, at the cost
    of conflating "a b" and "ab" — accepted, because `_MatchPool.claim` still
    resolves that ambiguity the same way as an exact duplicate: earliest
    unlinked candidate, consumed in send order."""
    return re.sub(r"\s+", "", text or "")


class _MatchPool:
    """Server-side attribution's candidate pool for ONE `persist_transcript_rows`
    call — the earliest of this session's claimed, unlinked chat-send turns,
    each matched against a row's text at most once.

    Lazily loaded on the FIRST `claim()` call, never in `__init__`: a batch
    that is entirely vouched-marker or entirely genuinely-unattributable rows
    (the common cases) must cost nothing beyond that check, and a batch with
    no user rows at all must issue no query — matching a `_vouched_markers`'s
    own "only when the batch has user rows" rule. One query however many rows
    need it, because every `claim()` after the first is served from the same
    in-memory list.

    `claim()` removes a match from the pool — "consumed within the batch" —
    so two identical-text rows in one batch map to two different turns, in
    the order they appear, exactly like two identical replies from two
    different people ("yes", "yes") should. The same consumption order is
    what resolves the ambiguity `_squash` introduces (a turn "a b" and a turn
    "ab" now compare equal): whichever of the two is earliest and still
    unlinked wins, which is the best any text-only match can do — the marker
    path (m3, checked first and always preferred) is the exact answer for
    when that is not good enough."""

    def __init__(self, session):
        self._session = session
        self._loaded = False
        self._candidates: list[Turn] = []

    def _load(self) -> None:
        if self._loaded:
            return
        self._loaded = True
        cutoff = timezone.now() - _dt.timedelta(days=7)
        already_linked = Message.objects.filter(
            session=self._session, source_turn_id__isnull=False
        ).values("source_turn_id")
        turns = (
            Turn.objects.filter(
                chat_session=self._session, claimed_at__isnull=False, created_at__gte=cutoff,
            )
            .exclude(pk__in=already_linked)
            .select_related("initiator_user", "initiator_contact")
            .order_by("created_at")
        )
        self._candidates = [t for t in turns if authorship.is_chat_send(t)]

    def claim(self, text: str) -> Turn | None:
        """The earliest remaining candidate whose prompt matches `text` with
        ALL whitespace ignored (see `_squash`) — or None. Removes it from the
        pool."""
        self._load()
        target = _squash(text)
        for i, turn in enumerate(self._candidates):
            if _squash(turn.prompt or "") == target:
                return self._candidates.pop(i)
        return None


def write_backfill(session, messages) -> int:
    """Write a runner's shipped full transcript as Message rows. Ordinal-keyed
    payloads (a current runner) upsert-fill: they add the older rows the live
    stream never saw and skip anything already persisted. A legacy payload (no
    ordinals) keeps the old write-once contract — sequential, and only into an
    empty session. messages: [{"role","text"[,"index"]}] chronological.

    `attribute=False`: a backfill ships a session's full history in one shot
    with no per-row timestamp, so server-side attribution's "earliest claimed
    turn" match (`persist_transcript_rows`) has no way to tell an old row from
    a new one — see that function's docstring. The vouched-marker path still
    runs (it names an exact turn id, not a guess), so a legacy marked row
    backfilled from before this change is still attributed correctly."""
    ordinal = any(int(m.get("index", -1)) >= 0 for m in messages)
    if not ordinal and Message.objects.filter(session=session).exists():
        return 0
    return persist_transcript_rows(session, messages, attribute=False)


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


def seed_stream_desired(binding) -> None:
    """A viewer who arrived BEFORE the session had a runner binding still gets streamed to.

    `attach_session` marks a binding `stream_desired` only on the 0->1 viewer
    edge, and only if a binding exists at that moment. A brand-new chat has
    none yet: the embedded widget sends the first message, opens its socket and
    attaches within a second, while the runner binds the session a moment
    later when it claims the turn. The edge had already fired into nothing, the
    new binding started at False, and `post_session_stream` then persisted every
    event and pushed none — the viewer saw "Thinking…" and never their own
    words or the reply (connect-labs, 2026-09-25).

    So whoever creates or refreshes a binding calls this after saving it: if
    someone is watching and the binding does not know, it is told — and the
    runner with it, on commit, since the caller is usually inside an atomic
    block. A no-op otherwise, which makes it safe on every save.
    """
    from django.db import transaction

    from . import attach

    if binding.stream_desired or attach.count(binding.session_id) <= 0:
        return
    session = binding.session
    transaction.on_commit(lambda: _set_stream_desired(session, True))


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
    "email_thread_key", "via", "capability",
    # A host's runner requirements (ZDR, apps/harness/runner_requirements.py),
    # copied from the token that started or sent into the session.
    "runner_requirements",
})
MAX_HOST_METADATA_KEYS = 20
MAX_HOST_METADATA_BYTES = 4096


def host_metadata(raw) -> dict:
    """What a caller may put on a session: their own descriptive keys (ace-web's
    `origin_key`, `opp_slug`, …), never one canopy acts on. ONE rule for every
    principal — a signed-in user, a PAT, a widget contact — so a contact's
    conversation carries a host's link exactly as a user's does."""
    import json

    if not isinstance(raw, dict):
        return {}
    out = {str(k): v for k, v in raw.items() if str(k) not in SERVER_OWNED_METADATA}
    if len(out) > MAX_HOST_METADATA_KEYS or len(json.dumps(out, default=str)) > MAX_HOST_METADATA_BYTES:
        raise ValueError(f"session metadata is limited to {MAX_HOST_METADATA_KEYS} keys "
                         f"and {MAX_HOST_METADATA_BYTES} bytes")
    return out


def add_runner_requirements(session: Session, reqs) -> None:
    """Union `reqs` into the session's requirements. Never removes one: a
    conversation that held a host's data keeps the host's floor, whatever token
    touches it next (spec 2026-09-30-zdr-runners)."""
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
        if merged == sorted(current):
            return
        meta[rr.METADATA_KEY] = merged
        Session.objects.filter(pk=s.pk).update(metadata=meta)
    session.metadata = meta


def create_session(*, workspace, created_by=None, agent=None, project: str = "", title: str = "",
                   metadata: dict | None = None, contact=None) -> Session:
    """One way to start a conversation, whoever starts it. A USER owns it (a
    participant row, SP3 multiplayer); a CONTACT has no account, so the session
    records them on `contact` instead and has no creator — which is what keeps
    it out of every member's list (`access.contact_session_q`)."""
    # Atomic so a session never exists without its owner. Local imports avoid a cycle.
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
            title=title, metadata=meta, contact=contact,
        )
        if created_by is not None:
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


def _index_offset(session) -> int:
    """This session's current transcript epoch base — see
    `RunnerBinding.index_offset`. No binding means nothing has ever been
    transferred, so 0."""
    binding = RunnerBinding.objects.filter(session=session).values_list(
        "index_offset", flat=True
    ).first()
    return int(binding or 0)


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
    from apps.harness import runner_requirements as rr

    # A box the conversation's host does not allow is no placement at all: a pin
    # to it would sit unclaimable forever (claim_next_turn refuses it above pins).
    if not rr.satisfies(runner.flags, rr.requirements_of_session(session)):
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



# Prepended to every transfer brief, server-side, so no caller can forget it.
#
# The receiving session is NOT a resumed conversation. A transfer re-points the
# binding at a box that cannot reach the old box's claude session, so the new
# runner opens a fresh one: it arrives with none of the thread above it in
# context, while the web UI shows it the whole history — which reads, to it, as
# if it should already know all of this. Saying so plainly is what stops it
# confidently re-deriving (or redoing) work that already shipped. Proven by hand
# on 2026-09-12: a transferred session given this preamble plus a git-state brief
# verified two PRs and a file checksum and then correctly stopped, rather than
# starting over.
TRANSFER_PREAMBLE = """\
**This session has been transferred from {source} to {target}.**

You are a FRESH session picking up this thread. The conversation above happened \
on another box, in a different checkout — you do NOT have it in context, even \
though the chat history is showing it to you. Everything you can rely on is \
below. Read it, verify the state yourself, then report and wait; do not start \
new work or redo anything already described as shipped.

Anything that was never pushed from {source} did not come with you.
"""


def _initiator(initiator, user, via: str):
    """The turn's asker (`apps/harness/initiator.py`). Callers pass it, because
    only the caller knows how the person authenticated. A caller that has not
    been taught to still gets the right PERSON with an empty assurance — true
    about who, silent about how — rather than a guessed grade."""
    if initiator is not None:
        return initiator
    from apps.harness import initiator as who
    return who.for_user(user, via=via, assurance="")


def transfer_session(*, session: Session, placement: str, brief: str = "", user=None,
                     initiator=None):
    """Move a live session onto another runner, carrying its history.

    The three things a transfer has to do, which pinning a turn alone does NOT:

    1. **Re-point the binding.** A pinned turn lands on the target box, but the
       binding still names the old one, so `post_session_stream`'s
       runner-owned-binding gate (404s a non-owner) drops everything the new box
       ships until its first `record_session` happens to fix it up. Moving it here
       makes the transfer the thing that decides placement, rather than a race.
    2. **Open a new epoch.** The target cannot resume the source's claude session,
       so it starts a fresh transcript whose ordinals restart near 0. Those would
       collide with the rows already held and, worse, trip
       `ensure_transcript_identity` into deleting them. `index_offset` lifts the
       new box's ordinals above the inherited history instead. See its docstring
       on the model — this is the field's entire reason to exist.
    3. **Hand over context.** The new session is cold. `TRANSFER_PREAMBLE` plus the
       caller's `brief` become the prompt of a turn pinned to the target, which is
       the only thing standing between "picked up the thread" and "started again
       from a blank worktree".

    `placement` is a runner UUID (resolved through the same `_placeable_runner`
    gate `send_message` uses, so a foreign or non-session-capable box is refused
    rather than left pinned-and-unclaimable forever).

    Raises ValueError for an unresolvable/ineligible target, LookupError for a
    session with no binding to move, and RuntimeError while a turn is still
    executing — a box mid-thought would keep writing into the epoch we are about
    to close, so the caller stops the session first (`POST /{id}/stop`) and
    retries. Returns (binding, turn).
    """
    target = _placeable_runner(session, placement)
    if target is None:
        raise ValueError("unknown runner for transfer")
    if session.status != Session.ACTIVE:
        raise ValueError("cannot transfer an archived session")
    if Turn.objects.filter(
        chat_session=session, status__in=list(harness_services.EXECUTING)
    ).exists():
        raise RuntimeError("a turn is still executing — stop the session first")

    with transaction.atomic():
        binding = (
            RunnerBinding.objects.select_for_update().filter(session=session).first()
        )
        if binding is None:
            raise LookupError("session has no runner binding to transfer")
        source = binding.runner
        if source is not None and source.id == target.id:
            raise ValueError(f"session is already on '{target.name}'")

        # Round UP to a stride boundary past the high-water mark, so a stored index
        # minus the offset still decomposes into (record, block). `+ BLOCK_STRIDE`
        # rather than a bare ceiling leaves one clear stride of gap between the
        # epochs — without it an offset landing exactly on the mark lets the new
        # transcript's record 0 sit in the same stride as the old tail.
        high = Message.objects.filter(session=session).aggregate(m=Max("turn_index"))["m"]
        if high is not None:
            binding.index_offset = ((int(high) // BLOCK_STRIDE) + 2) * BLOCK_STRIDE

        binding.runner = target
        binding.transferred_from = source
        binding.transferred_at = timezone.now()
        # Everything below describes the SOURCE box's screen, and none of it is
        # true of the target. `session_key` and `host` in particular are what
        # `reusable_by` consults: left populated they would tell the target to go
        # drive a task that does not exist on it. Cleared, `resolve_session`
        # returns reuse=False + new_thread=False, which is precisely "open a fresh
        # session under this account and rehydrate from `summary`" — the path the
        # two-account failover already uses.
        binding.session_key = ""
        binding.host = ""
        binding.transcript_id = ""
        binding.pending_question = None
        binding.pending_answer = None
        binding.close_requested = False
        binding.agent_status = ""
        binding.agent_status_stale = False
        binding.tail = []
        binding.save(update_fields=[
            "runner", "transferred_from", "transferred_at", "index_offset",
            "session_key", "host", "transcript_id", "pending_question",
            "pending_answer", "close_requested", "agent_status",
            "agent_status_stale", "tail", "updated_at",
        ])

        # So a LATER send on a still-unbound session re-pins here too, instead of
        # falling back to open routing and landing on whichever box polls first.
        metadata = dict(session.metadata or {})
        metadata["requested_runner_id"] = str(target.id)
        session.metadata = metadata
        session.save(update_fields=["metadata", "updated_at"])

        source_name = source.name if source is not None else "an unknown runner"
        prompt = TRANSFER_PREAMBLE.format(source=source_name, target=target.name)
        if brief.strip():
            prompt = f"{prompt}\n{brief.strip()}\n"
        thread_key = binding.thread_key or str(session.id)
        turn, _created = harness_services.enqueue_turn(
            session=session,
            origin=Turn.ORIGIN_CANOPY_WEB_CHAT,
            # Keyed on the TARGET and the transfer's own timestamp: transferring
            # back and forth (which is the normal shape of a failover) must not
            # dedupe onto the outbound turn.
            idempotency_key=(
                f"transfer:{session.id.hex}:{target.id.hex}:"
                f"{int(binding.transferred_at.timestamp())}"
            ),
            prompt=prompt,
            origin_ref={"thread_key": thread_key, "chat_session_id": str(session.id),
                        "transfer_from": source_name},
            enqueued_by=user,
            pinned_runner=target,
            initiator=_initiator(initiator, user, "transfer"),
        )
    return binding, turn


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
    capability: str | None = None,
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
            placement=placement, origin=origin, initiator=initiator,
            origin_ref=origin_ref, capability=capability,
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
        ref = _merge_origin_ref(origin_ref, thread_key=thread_key, session=session)
        if client_id:
            # The one place a send's client nonce survives as itself: the
            # idempotency key's suffix is an index when there is none, and
            # `queued_messages` must not report an index as a client_id.
            ref["client_id"] = client_id
        attachments = claim_pending_attachments(session, message, user)
        if attachments:
            ref["attachments"] = attachments
        turn, _created = harness_services.enqueue_turn(
            session=session,
            origin=origin,
            idempotency_key=f"chat:{session.id.hex}:{client_id or index}",
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
            initiator=_initiator(initiator, user, origin),
            capability=capability,
        )
        # The ledger path writes its own row, so it records the author directly
        # rather than through the transcript marker.
        if user is not None and getattr(user, "is_authenticated", False):
            message.author = {"name": (user.get_full_name() or "").strip() or user.email,
                              "user_id": user.pk}
        if turn is not None:
            message.source_turn_id = turn.pk
        message.save(update_fields=["author", "source_turn_id"])
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


def available_cloud_runner(session: Session):
    """An ONLINE, session-capable cloud runner this session could be placed on,
    or None. Same `_placeable_runner` gate as every other placement, so the
    answer is never a box that would leave the turn pinned and unclaimable."""
    from apps.harness.models import Runner

    for runner in (Runner.objects.filter(kind=Runner.CLOUD, paired_by__isnull=False)
                   .exclude(status=Runner.RETIRED).order_by("name")):
        if runner.live_status != Runner.ONLINE:
            continue
        if _placeable_runner(session, str(runner.id)) is not None:
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
    target = _placeable_runner(session, placement)
    if target is None:
        raise ValueError("unknown runner")
    queued = list(Turn.objects.filter(chat_session=session, status=Turn.QUEUED).order_by("created_at"))
    if not queued:
        raise LookupError("no queued turn to move")
    binding = RunnerBinding.objects.select_related("runner").filter(session=session).first()
    if binding is not None and binding.runner_id and binding.runner_id != target.id:
        _binding, handoff = transfer_session(
            session=session, placement=str(target.id),
            brief=MOVE_BRIEF.format(source=binding.runner.name, n=len(queued)),
            user=user, initiator=initiator,
        )
        Turn.objects.filter(pk=handoff.pk).update(
            created_at=queued[0].created_at - _dt.timedelta(milliseconds=1))
    else:
        metadata = dict(session.metadata or {})
        metadata["requested_runner_id"] = str(target.id)
        session.metadata = metadata
        session.save(update_fields=["metadata", "updated_at"])
    Turn.objects.filter(pk__in=[t.pk for t in queued], status=Turn.QUEUED).update(pinned_runner=target)
    for t in queued:
        t.refresh_from_db()
    return [t for t in queued if t.pinned_runner_id == target.id]


def _send_transcript_sourced_message(
    *, session: Session, text: str, user=None, client_id: str = "",
    placement: str | None = None, origin: str = Turn.ORIGIN_CANOPY_WEB_CHAT,
    initiator=None, origin_ref: dict | None = None, capability: str | None = None,
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
    ref = _merge_origin_ref(origin_ref, thread_key=thread_key, session=session)
    if client_id:
        ref["client_id"] = client_id   # see send_message: the key suffix may be a nonce
    # message=None: this path writes no durable user row, so the sent_at stamp is
    # the only thing stopping these attachments riding along on every later send.
    attachments = claim_pending_attachments(session, None, user)
    if attachments:
        ref["attachments"] = attachments
    turn, _created = harness_services.enqueue_turn(
        session=session,
        origin=origin,
        idempotency_key=f"chat:{session.id.hex}:{client_id or uuid.uuid4().hex}",
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
        initiator=_initiator(initiator, user, origin),
        capability=capability,
    )
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
    # The REPORTED branch announces itself when the runner's report retires the
    # row; this one never gets a report, so it says so here.
    from apps.harness.services import fire_sessions_closed

    transaction.on_commit(lambda: fire_sessions_closed([session.pk]))
    return "closed"

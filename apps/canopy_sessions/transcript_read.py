"""Reading a session's transcript: the tail and scroll-back, which rows a
person typed, what a list shows (opening, last reply, activity), whether a
session is running, and asking a runner for a backfill.
"""
from __future__ import annotations

import datetime as _dt
import logging
from dataclasses import dataclass

from canopy_transcript import BLOCK_STRIDE
from django.db.models import OuterRef, Subquery
from django.utils import timezone

from apps.harness.models import Turn

from . import authorship, staleness, transcript_ingest
from .models import Message, Session
from .transcript_noise import is_system_noise

logger = logging.getLogger(__name__)

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


def _normalized_prompt(text: str) -> str:
    return authorship.without_attachment_note(text or "").strip()


def program_prompts(session: Session) -> set[str]:
    """The prompts PROGRAMS delivered into this session, normalized.

    A USER row is whatever arrived on the human's side of the transcript, and
    not all of it was typed by a person: a scheduled turn (`/hal:turn`), an
    email turn, an MCP/API caller or a transfer preamble lands there too. The
    turns that drove the session say which: a chat send (`authorship.
    is_chat_send`) is a person; every other turn on the session — a chat-bound
    one, or an agent turn the runner opened this session for (same agent and
    `session_key`, the `with_driving_turn` rule) — is a program, and its prompt
    is what its USER row reads.
    """
    from django.db.models import Q

    match = Q(chat_session=session)
    binding = getattr(session, "runner_binding", None)
    if binding is not None and binding.session_key and session.agent_id:
        match |= Q(agent_id=session.agent_id, session_key=binding.session_key)
    turns = Turn.objects.filter(match).only(
        "prompt", "origin", "idempotency_key", "chat_session_id",
        "initiator_user_id", "initiator_contact_id",
    )
    return {_normalized_prompt(t.prompt) for t in turns
            if t.prompt and not authorship.is_chat_send(t)}


#: A user record's `promptSource` when a PROGRAM put it there: an agent's own
#: ScheduleWakeup/cron fire, a peer session's message, a task notification, the
#: usage-limit auto-continue. "typed", "queued" (typed while a turn ran) and
#: "sdk" are a person.
_PROGRAM_PROMPT_SOURCES = frozenset({"system"})

#: LAST-RESORT text shapes, used only for a row with no captured provenance (one
#: shipped before runners kept `content["record"]`): what the harness writes on
#: the human's side that no structural signal on the server can identify. Each
#: was measured against the transcripts' own flags (2026-10-11, 840 sessions).
#: A compaction summary may open with a preamble
#: ("<artifact-content-authored-by-others/>"), so it is looked for in a window.
_COMPACTION_SHAPE = "this session is being continued from a previous conversation"
_SHAPE_WINDOW = 400
_NOT_TYPED_PREFIXES = (
    "another claude session sent a message:",
    "your claude.ai usage limit has reset. continue the task",
    "[usage limit reached;",
    "[earlier usage-limit notes",
    "[cross-session delivery notice]",
    # Beside a person's attached image / around a local command: the person's
    # own words are a separate row; these are Claude Code's annotations.
    "[image: source: ",
    "<local-command-caveat>",
)
_NOT_TYPED_EXACT = ("continue from where you left off.",)
#: A tool-carried prompt shorter than this is not matched: "keep going" is as
#: likely typed as scheduled, and dropping a person's words is the worse error.
_DISPATCHED_MIN = 40
_TRUNCATION_MARK = "\n… [truncated "


def record_flags(message) -> dict | None:
    """The provenance a runner captured from the transcript record behind this
    row (`canopy_transcript.record_flags`), or None for a row shipped before
    runners captured it — the cue to fall back to `InjectedPrompts`."""
    content = getattr(message, "content", None)
    flags = content.get("record") if isinstance(content, dict) else None
    return flags if isinstance(flags, dict) else None


def typed_by_flags(flags: dict) -> bool:
    """Do the record's own flags say a person typed it? Not a slash command's
    or skill's injected body (`meta`, `source_tool_use_id`), not a subagent's
    record (`sidechain`), not a compaction summary, and not a prompt a program
    put there (`prompt_source` "system")."""
    return not (
        flags.get("meta") or flags.get("sidechain") or flags.get("compact_summary")
        or flags.get("source_tool_use_id")
        or flags.get("prompt_source") in _PROGRAM_PROMPT_SOURCES
    )


class InjectedPrompts:
    """For rows WITHOUT captured provenance: what the session's own stored rows
    prove was injected rather than typed. Structural, from the server's copy of
    the transcript, and loaded lazily — a session whose rows all carry flags
    never pays for it.

    - `exact`/`prefixes`: every prompt a tool call carried (`input.prompt`;
      a prefix when the producer capped it). An
      agent's `ScheduleWakeup`/`CronCreate` fire and an `Agent`/`Task`
      dispatch arrive later as a user row with exactly that text.
    - `expansion_records`: the record ordinal straight after a `Skill` call's
      result, or after a `<command-name>` row — where Claude Code writes the
      skill's or slash command's whole body as a user record. (The command
      row itself is what the person typed and stays.)
    """

    def __init__(self, session: Session):
        self.session = session
        self._loaded = False

    def _load(self):
        if self._loaded:
            return
        rows = self.session.messages
        self.exact: set[str] = set()
        self.prefixes: list[str] = []
        for prompt in rows.filter(role=Message.TOOL_USE, content__input__has_key="prompt") \
                .values_list("content__input__prompt", flat=True):
            if not isinstance(prompt, str) or not prompt.strip():
                continue
            if _TRUNCATION_MARK in prompt:   # capped at the producer (rows.TOOL_INPUT_STR_MAX)
                self.prefixes.append(transcript_ingest._squash(prompt.split(_TRUNCATION_MARK, 1)[0]))
            else:
                self.exact.add(transcript_ingest._squash(prompt))
        skill_ids = [i for i in rows.filter(role=Message.TOOL_USE, content__name="Skill")
                     .values_list("content__id", flat=True) if i]
        skill_parents = list(rows.filter(role=Message.TOOL_RESULT, content__tool_use_id__in=skill_ids)
                             .values_list("turn_index", flat=True)) if skill_ids else []
        parents = skill_parents + list(rows.filter(role=Message.USER, plaintext__regex=r"^\s*<command-(message|name)>")
                        .values_list("turn_index", flat=True))
        self.expansion_records = {i // BLOCK_STRIDE + 1 for i in parents}
        # A skill RE-invoked in one session writes two records after its result:
        # a "(Re-invocation of /x …)" note, then the body.
        self.expansion_records |= {i // BLOCK_STRIDE + 2 for i in skill_parents}
        self._loaded = True

    def injected(self, message) -> bool:
        self._load()
        if message.turn_index // BLOCK_STRIDE in self.expansion_records:
            return True
        text = transcript_ingest._squash(_normalized_prompt(message.plaintext))
        if len(text) >= _DISPATCHED_MIN and (
                text in self.exact or any(len(p) >= 150 and text.startswith(p) for p in self.prefixes)):
            return True
        # `_squash` is whitespace-BLIND (see its docstring), so the shapes are too.
        head = text[:_SHAPE_WINDOW].lower()
        return (transcript_ingest._squash(_COMPACTION_SHAPE) in head
                or any(head.startswith(transcript_ingest._squash(p)) for p in _NOT_TYPED_PREFIXES)
                or head in {transcript_ingest._squash(e) for e in _NOT_TYPED_EXACT})


def is_human_input(message, program: set[str], injected: InjectedPrompts | None = None) -> bool:
    """Is this row something a person typed?

    A USER row, not harness noise (`is_system_noise` — re-applied on read, since
    rows persisted before a prefix was added still carry it), and then:

    1. **Provenance, when captured** (`record_flags`): the transcript record's
       own flags decide — they are the ground truth, see `typed_by_flags`.
    2. **Otherwise the session's own rows** (`InjectedPrompts`): a prompt a
       tool call carried, a skill/command body, and — last resort — the text
       shapes of a compaction summary, peer message or auto-continue.

    Either way a row attributed to a person (`author`) is theirs, and one whose
    text is a program's prompt (a scheduled/email turn, an API caller) is not.
    """
    if getattr(message, "role", None) != Message.USER:
        return False
    text = message.plaintext or ""
    if is_system_noise(text):
        return False
    flags = record_flags(message)
    if flags is not None:
        if not typed_by_flags(flags):
            return False
    elif injected is not None and injected.injected(message):
        return False
    if getattr(message, "author", None):
        return True
    return _normalized_prompt(text) not in program


def human_inputs(session: Session, *, after: int | None = None, limit: int = SCROLLBACK_PAGE_DEFAULT):
    """What PEOPLE typed into this session, oldest first, `limit` at a time.

    Returns (messages, next_cursor, source). `after` is an exclusive turn_index
    cursor (None = from the start); `next_cursor` is the turn_index to pass back,
    or None when nothing later exists. Tool results, the agent's own output,
    system rows, harness records and program prompts are all excluded — see
    `is_human_input`.

    A session with no durable rows yet (a local runner session before its
    backfill) answers from the binding's rolling tail, source "tail", unpaged.
    """
    program = program_prompts(session)
    injected = InjectedPrompts(session)
    users = session.messages.filter(role=Message.USER).order_by("turn_index")
    if after is None and not session.messages.exists():
        tail = [m for m in tail_as_messages(session, getattr(session, "runner_binding", None))
                if is_human_input(m, program)]
        return tail[-limit:], None, "tail"
    out: list = []
    cursor = after
    # Scan in chunks: filtering is per-row in Python (the noise rule is a prefix
    # list shared with the runners, not a query), so a chunk may yield fewer
    # than `limit`; keep going until the page is full or the rows run out.
    chunk = max(limit, 50)
    while len(out) < limit:
        qs = users if cursor is None else users.filter(turn_index__gt=cursor)
        batch = list(qs[:chunk])
        if not batch:
            return out, None, "transcript"
        for m in batch:
            cursor = m.turn_index
            if is_human_input(m, program, injected):
                out.append(m)
                if len(out) == limit:
                    break
    more = users.filter(turn_index__gt=cursor).exists()
    return out, (cursor if more else None), "transcript"


#: What an embedded widget never receives: the agent's tool calls and their
#: results. A widget's visitor asked a question and wants the answer; which MCP
#: tools ran, with what arguments, is noise to them and often a page of JSON.
#: Not a host option — a widget cannot ask for them. The transcript keeps every
#: call, and canopy's own chat page shows them.
WIDGET_HIDDEN_ROLES = frozenset({Message.TOOL_USE, Message.TOOL_RESULT})


def for_widget(messages):
    """`messages` as an embedded widget may see them — without tool rows.

    Except a tool result that carries an MCP Apps View (`app`, set by
    `mcp_apps_host.mcp_apps_views.annotate`): the View IS the host's UI for the person
    on the page, so it reaches the widget — stripped to its call id, never the
    raw result (the View reads what it needs as the viewer)."""
    from apps.mcp_apps_host.mcp_apps_views import strip_for_widget

    return [strip_for_widget(m) if getattr(m, "app", None) else m for m in messages
            if getattr(m, "role", None) not in WIDGET_HIDDEN_ROLES or getattr(m, "app", None)]


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


_SPOKEN_ROLES = (Message.USER, Message.ASSISTANT)


def with_last_reply(sessions):
    """`sessions` annotated with `_last_speaker` (role of the newest user/agent
    row) and `_last_reply` (text of the newest agent row) — the supervisor feed's
    "is it my turn, and what did it say?", in the same query as the list."""
    spoken = (
        Message.objects.filter(session=OuterRef("pk"), role__in=_SPOKEN_ROLES)
        .exclude(plaintext="")
        .order_by("-turn_index")
    )
    return sessions.annotate(
        _last_speaker=Subquery(spoken.values("role")[:1]),
        _last_reply=Subquery(spoken.filter(role=Message.ASSISTANT).values("plaintext")[:1]),
    )


def with_driving_turn(sessions):
    """`sessions` annotated with `_turn_mode` / `_turn_origin`: the mode and origin
    of the FIRST claimed turn that drove each one — the session's mode is fixed
    when it starts (apps/harness/turn_mode.py), so the feed's "did the agent do
    this on its own?" and the list's mode badge read how it started, not
    whoever replied last.

    A turn drives a session two ways: a chat send carries the session itself
    (`chat_session`), and an agent turn the runner opened a session for carries
    that session's key (`Turn.session_key`, the same value as the binding's) —
    which is how a scheduled or email turn's session is found, since it was never
    a chat. Unclaimed turns have no mode yet ("" until claim), so they say nothing.
    """
    driving = staleness.driving_turns().exclude(turn_mode="").order_by("created_at")
    return sessions.annotate(
        _turn_mode=Subquery(driving.values("turn_mode")[:1]),
        _turn_origin=Subquery(driving.values("origin")[:1]),
    )


def with_open_turn(sessions):
    """`sessions` annotated with `_turn_open`: a turn that drives each one is
    queued or in flight (`Turn.NON_TERMINAL`). The feed's evidence that a session
    on a paused/offline runner is genuinely held up (`feed.status`)."""
    from django.db.models import Exists

    return sessions.annotate(
        _turn_open=Exists(staleness.driving_turns().filter(status__in=Turn.NON_TERMINAL)),
    )


def last_reply_from(rows) -> tuple[str, bool]:
    """(the agent's last reply, did the agent have the last word) from
    chronological (role, text) pairs. Tool and system rows are not speech.

    The reply is WHOLE, never trimmed: the feed renders it in place, and a cap
    here (it was 2400 chars) cut off the end of every long answer with no way to
    expand past it. The list sends it only when it is the person's turn
    (api._out), which is what bounds the payload."""
    spoken = [(r, t) for r, t in rows if r in _SPOKEN_ROLES and (t or "").strip()]
    if not spoken:
        return "", False
    reply = next((t for r, t in reversed(spoken) if r == Message.ASSISTANT), "")
    return reply.strip(), spoken[-1][0] == Message.ASSISTANT


def last_reply_of(session, binding) -> tuple[str, bool]:
    """The feed's reply for one session (needs `with_last_reply` for web chats).

    The runner-reported tail wins when there is one: a discovered session holds
    no Message rows until somebody opens it, and the tail is refreshed on every
    report — so an emdash task that finished while nobody watched still shows
    what it said.
    """
    tail = tail_as_messages(session, binding)
    if tail:
        return last_reply_from([(m.role, m.plaintext) for m in tail])
    speaker = getattr(session, "_last_speaker", None)
    if speaker is None:
        return "", False
    return (getattr(session, "_last_reply", None) or "").strip(), speaker == Message.ASSISTANT


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


def with_activity(sessions):
    """Annotate `_last_msg_at` and `_activity`: `last_activity_at`'s rule (binding >
    newest message > created) in SQL, so a list can window, sort or aggregate on it.
    The one SQL spelling, for the session list and the workspace health dashboard."""
    from django.db.models import Max
    from django.db.models.functions import Coalesce

    return sessions.annotate(_last_msg_at=Max("messages__created_at")).annotate(
        _activity=Coalesce("runner_binding__last_interacted_at", "_last_msg_at", "created_at"))


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


class BackfillRefusedError(Exception):
    """This session's transcript is never asked for: its box is a sensitive-data
    runner (`runner_requirements.is_sensitive_data_box`)."""


SENSITIVE_BOX_BACKFILL_REFUSAL = (
    "This session's runner is a sensitive-data box: it keeps its transcripts to itself "
    "and deletes each turn's when the turn ends, so its history is never asked for.")


def backfill_refused(binding) -> bool:
    """Whether a backfill must never be asked of this binding's box: a
    sensitive-data runner ships no transcript, whatever the session."""
    from apps.harness import runner_requirements

    runner = getattr(binding, "runner", None) if binding is not None else None
    return runner is not None and runner_requirements.is_sensitive_data_box(runner)


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
    (`backfillAction`) for old servers.

    Raises BackfillRefusedError when the bound box is a sensitive-data runner."""
    from apps.canopy_sessions.models import RunnerBinding

    binding = RunnerBinding.objects.select_related("runner").filter(session=session).first()
    if backfill_refused(binding):
        raise BackfillRefusedError(SENSITIVE_BOX_BACKFILL_REFUSAL)
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

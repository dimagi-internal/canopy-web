"""Bridge an emdash session's live response back into the harness ledger.

The laptop runner injects a chat prompt into an emdash session and then TAILS that
session's Claude Code transcript (.jsonl), posting each new assistant TEXT block as
an `assistant` TurnEvent — which the chat SessionConsumer translates to chat.stream_*
so the website streams the reply. This is the piece the normal agent/project path
deliberately omits (there the work just continues in the visible emdash session).

Completion is STRUCTURAL, read off the transcript's own end-of-turn marker (see
`hands_back_to_human`) — NOT "the file went quiet". It was idle-based until
2026-07-26, on the stated premise that Claude Code writes no turn-done marker; it
writes one on every assistant record (`message.stop_reason`), and the premise cost
us every answer worth reading. An agent turn is SILENT for as long as its longest
tool call — 296s in the session that exposed this — so a 3s quiet window meant the
first `Bash` call ended the turn: chat showed the agent's opening line, declared it
done, and dropped the actual answer on the floor. (Labs, 2026-07-26: 11 consecutive
turns finished in 14-60s having bridged 70-220 chars each — all preambles.)

A turn therefore outlives the runner tick that started it. `LiveBridge` holds that
state between ticks and `chat_pump.pump_chat_bridges` advances it, so the runner keeps
heartbeating and claiming while an agent works. The step function stays pure
(records in, texts out) so the state machine unit-tests without files or a clock.
"""
from __future__ import annotations

from dataclasses import dataclass, field

# The transcript core lives in canopy_transcript so the cloud runner shares it
# verbatim (spec 2026-07-27). These re-exports keep existing call sites and
# tests importing from chat_bridge working — this module is now the emdash
# BRIDGE state machine, and nothing else.
from canopy_transcript import (  # noqa: F401
    BLOCK_STRIDE,
    TOOL_INPUT_JSON_MAX,
    TOOL_INPUT_STR_MAX,
    TOOL_TEXT_MAX,
    TRANSCRIPT_BATCH_MAX_BYTES,
    assistant_text as _assistant_text,
    chunk_raw_lines,
    compose_index,
    conversational_messages,
    end_index,
    read_records,
    row_payload,
    scrub,
    user_text as _user_text,
)

# In-flight chat bridges, keyed by turn_id: {turn_id: LiveBridge}. Module-level so
# execute.py can register one and main.py can pump it without an import cycle
# (both already import this module), matching how main keeps _tail_readers /
# _stream_readers / CANCELLED_TURNS. A runner restart drops the registry: those
# turns stay EXECUTING until the server's lease sweep reclaims them — the same
# outcome a restart mid-bridge had before.
IN_FLIGHT: dict[str, LiveBridge] = {}


def new_assistant_texts(records: list[dict], since: int) -> list[str]:
    """Assistant TEXT messages in records[since:], oldest->newest, non-empty only.

    Prose only, deliberately: the bridge's events go to the turn ledger while the
    same records also reach the client with tool rows down the durable stream
    path, so emitting tools here too would render each call twice under two
    message ids. `canopy_transcript.rows_for_record` is the full-fidelity reader.
    """
    texts: list[str] = []
    for rec in records[since:]:
        if rec.get("type") != "assistant":
            continue
        msg = rec.get("message")
        content = msg.get("content", "") if isinstance(msg, dict) else ""
        t = _assistant_text(content)
        if t:
            texts.append(t)
    return texts


def hands_back_to_human(rec: dict) -> bool:
    """True when this record ENDS the agent's turn — the floor is back with the human.

    Claude Code stamps every assistant record with the API's `stop_reason`.
    "tool_use" means "I'm calling a tool and will continue after its result"; every
    other terminal value ("end_turn", "stop_sequence", "max_tokens", a refusal)
    means the model stopped and is waiting on a person. That distinction is the ONLY
    completion signal immune to how long a tool takes, which is what makes it the
    right one: silence means a tool is running, never that the turn is over.

    A missing/None stop_reason is NOT an ending — a writer that omits the field
    leaves us on the idle backstop rather than ending the turn on every record.
    """
    if rec.get("type") != "assistant":
        return False
    msg = rec.get("message")
    reason = msg.get("stop_reason") if isinstance(msg, dict) else None
    return isinstance(reason, str) and reason != "tool_use"


# Backstops, in PUMP TICKS (one per runner loop iteration, ~5s at the default
# poll_seconds) — deliberately counted in ticks, not wall-clock, so the state
# machine stays deterministic under an injected clock.
#
# IDLE_TICKS is "the transcript produced NOTHING for this long", not "the agent is
# thinking": it exists only for a writer that never stamps a stop_reason, and for a
# session whose injection silently never landed. It must stay far longer than any
# plausible tool call (the old 3s is what broke this) while staying under the
# server's 900s turn lease... which the heartbeat renews for as long as we report
# the turn as active, so the real ceiling is "before a human gives up".
IDLE_TICKS = 180          # ~15 min of a completely silent transcript
MAX_TICKS = 2880          # ~4 h total, so a wedged bridge can't hold a session forever

#: Reported on the heartbeat as `midturn`: this code delivers a follow-up INTO a
#: running chat turn (`Rider`, canopy-web#1153). canopy-web hands a rider only to
#: a runner reporting it — older code would bridge the same reply twice.
MIDTURN_VERSION = 1

#: What Claude Code's background-task notices look like when they pass through
#: its prompt queue. They are the agent's own plumbing, never a person's message,
#: so they must not count as a rider arriving.
_TASK_NOTICE = "<task-notification"


def _norm(s: str) -> str:
    return " ".join((s or "").split())


def received_prompt(rec: dict) -> str | None:
    """The text of a person's message that Claude Code has just HANDED TO THE
    MODEL, or None if `rec` is not that.

    Typed into a busy session, a message is queued (`queue-operation` enqueue)
    and then taken one of two ways, both measured in this repo's own sessions
    (2026-10-05, the #1147 incident transcript): mid-turn it is `remove`d from
    the queue and injected as a `queued_command` attachment the model reads at
    its next step; once the turn has ended it is `dequeue`d and arrives as an
    ordinary `user` prompt. The enqueue itself is NOT receipt — the model has
    not seen it yet. Neither is the `remove`: it always precedes the
    attachment, so counting both would receive one message twice."""
    kind = rec.get("type")
    text = None
    if kind == "attachment":
        att = rec.get("attachment") or {}
        if att.get("type") == "queued_command" and isinstance(att.get("prompt"), str):
            text = att["prompt"]
    elif kind == "user" and not rec.get("isMeta"):
        msg = rec.get("message")
        content = msg.get("content") if isinstance(msg, dict) else None
        if isinstance(content, str):
            text = content
        elif isinstance(content, list) and not any(
                isinstance(b, dict) and b.get("type") == "tool_result" for b in content):
            text = "\n".join(b.get("text", "") for b in content
                             if isinstance(b, dict) and b.get("type") == "text")
    if text is None or text.lstrip().startswith(_TASK_NOTICE):
        return None
    return text


@dataclass
class Rider:
    """A follow-up typed into this bridge's session while its turn was running
    (canopy-web#1153). Its reply is part of this turn's reply, so the turn must
    not end until the model has actually RECEIVED it."""

    turn_id: str
    probe: str
    received: bool = False


@dataclass
class LiveBridge:
    """One chat turn being bridged, ACROSS runner ticks.

    Holds the between-tick state the old inline loop kept on its stack. `reader` is
    anything with `read_new() -> list[dict]` (a `tail.TailReader` in production, a
    list-popping stub in tests); the pump owns the I/O, this owns the decisions.

    `pending` is the retry queue: text is only dropped once the server has taken it,
    so a transient POST failure delays a line instead of losing it — and the turn
    never finishes with text still undelivered.
    """

    turn_id: str
    task: str
    reader: object
    pending: list[str] = field(default_factory=list)
    collected: list[str] = field(default_factory=list)
    idle_ticks: int = 0
    ticks: int = 0
    done_reason: str = ""
    # Raw JSONL awaiting a flush to POST /turns/{id}/transcript, and the count of
    # batches already accepted (the batch_id's sequence number, so a lost-ack
    # retry dedupes server-side instead of double-appending).
    raw_pending: list[str] = field(default_factory=list)
    raw_batches_sent: int = 0
    transcript_truncated: bool = False
    # Stops attempted on this turn whose interrupt did NOT confirm. Between-tick
    # state for the same reason everything else here is: `cancel_chat_bridge` retries
    # the Escape across ticks rather than finishing on the first unconfirmed press.
    cancel_attempts: int = 0
    # The emdash project that owns `task`. Task names are unique per project only,
    # so the stop's Escape must be aimed by (project, task) — see cdp_control.interrupt.
    project: str = ""
    # Follow-ups delivered into this turn (`add_rider`), in delivery order.
    riders: list[Rider] = field(default_factory=list)

    def add_rider(self, turn_id: str, prompt: str) -> None:
        """A follow-up was typed into this session mid-turn. The turn now runs
        until the model has received it AND handed the floor back after that.

        Re-opens a turn whose `end_turn` was already read but which is still
        flushing text: the follow-up's reply has not started yet."""
        from .delivery import probe_of

        self.riders.append(Rider(turn_id=turn_id, probe=probe_of(prompt)))
        if self.done_reason == "end_turn":
            self.done_reason = ""

    @property
    def awaiting_riders(self) -> bool:
        return any(not r.received for r in self.riders)

    def _note_received(self, text: str) -> None:
        """Match a received prompt to a waiting rider: by its words first; failing
        that, the oldest waiting one — Claude Code may rewrite what was typed
        (a collapsed paste, an attachment path), and a rider that can never be
        matched would hold the turn open to the idle backstop."""
        waiting = [r for r in self.riders if not r.received]
        if not waiting:
            return
        norm = _norm(text)
        match = next((r for r in waiting if r.probe and r.probe in norm), None)
        (match or waiting[0]).received = True

    def step(self, new_records: list[dict], raw_lines: list[str] | None = None,
             blocked: bool = False) -> None:
        """Consume one tick's worth of newly-appended records.

        `raw_lines` is the verbatim JSONL for those records (TailReader.last_raw).
        Optional so the state machine still unit-tests from records alone.

        `blocked` is "this session has a dialog up right now". It suppresses the
        idle backstop ONLY — an agent waiting on a human is silent by definition,
        so the one signal that says "this writer has stopped stamping stop_reason"
        cannot tell it apart from a wedge. Measured cost of not knowing: a turn
        finished `done` after 15 minutes carrying 936 characters of preamble while
        its dialog was still on screen, so the phone showed a completed chat whose
        answer never came (labs, 2026-08-01). MAX_TICKS still bounds it, so a
        dialog nobody ever answers cannot hold a session open forever.
        """
        self.ticks += 1
        if raw_lines and not self.transcript_truncated:
            self.raw_pending.extend(raw_lines)
        if new_records:
            self.idle_ticks = 0
        else:
            self.idle_ticks += 1
        texts = new_assistant_texts(new_records, 0)
        self.pending.extend(texts)
        self.collected.extend(texts)
        # In ORDER: a hand-back only ends the turn once every rider has been
        # received — one taken after this `end_turn` gets its own reply, which
        # ends with its own hand-back.
        for rec in new_records:
            text = received_prompt(rec) if self.riders else None
            if text is not None:
                self._note_received(text)
            if hands_back_to_human(rec) and not self.awaiting_riders:
                self.done_reason = "end_turn"
        if self.done_reason != "end_turn":
            if self.idle_ticks >= IDLE_TICKS and not blocked:
                self.done_reason = "idle"
            elif self.ticks >= MAX_TICKS:
                self.done_reason = "max_ticks"

    @property
    def finished(self) -> bool:
        """Done AND fully delivered — undelivered text keeps the turn open so the
        next tick can retry it.

        Deliberately does NOT wait on `raw_pending`: the retained transcript is a
        derived artifact, the reply is the turn's actual product, and holding a
        finished turn open over a transcript flush would make a storage hiccup
        look like an agent still working. The pump makes a final flush attempt at
        finish time instead."""
        return bool(self.done_reason) and not self.pending

    @property
    def note(self) -> str:
        chars = len("\n\n".join(self.collected))
        rode = f"; {len(self.riders)} follow-up(s) delivered mid-turn" if self.riders else ""
        if self.done_reason == "end_turn":
            return f"chat reply bridged ({chars} chars{rode})"
        return f"chat reply bridged ({chars} chars; ended on {self.done_reason}{rode})"

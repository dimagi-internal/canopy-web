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
state between ticks and `main._pump_chat_bridges` advances it, so the runner keeps
heartbeating and claiming while an agent works. The step function stays pure
(records in, texts out) so the state machine unit-tests without files or a clock.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

# In-flight chat bridges, keyed by turn_id: {turn_id: LiveBridge}. Module-level so
# execute.py can register one and main.py can pump it without an import cycle
# (both already import this module), matching how main keeps _tail_readers /
# _stream_readers / CANCELLED_TURNS. A runner restart drops the registry: those
# turns stay EXECUTING until the server's lease sweep reclaims them — the same
# outcome a restart mid-bridge had before.
IN_FLIGHT: dict[str, LiveBridge] = {}


def _assistant_text(content) -> str:
    """The assistant's spoken output — TEXT blocks only (tool_use blocks are skipped
    for the v1 bridge; the website shows the reply, not the tool calls)."""
    if isinstance(content, str):
        return content.strip()
    if not isinstance(content, list):
        return ""
    parts = [
        b.get("text", "")
        for b in content
        if isinstance(b, dict) and b.get("type") == "text"
    ]
    return "".join(parts).strip()


def read_records(path) -> list[dict]:
    """Every JSONL record in the transcript, best-effort (never raises)."""
    try:
        text = Path(path).read_text(errors="replace")
    except OSError:
        return []
    out: list[dict] = []
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            out.append(json.loads(line))
        except ValueError:
            continue
    return out


def new_assistant_texts(records: list[dict], since: int) -> list[str]:
    """Assistant TEXT messages in records[since:], oldest->newest, non-empty only."""
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


def _user_text(content) -> str:
    """A user record's text — a bare string, or the text blocks of a content list."""
    if isinstance(content, str):
        return content.strip()
    if isinstance(content, list):
        return "".join(
            b.get("text", "") for b in content
            if isinstance(b, dict) and b.get("type") == "text"
        ).strip()
    return ""


def conversational_messages(records: list[dict], since: int) -> list[dict]:
    """Conversational rows after `since`, as chronological {"index","role","text"}.

    `index` is the RAW position in the records list (== the .jsonl line ordinal;
    read_records reads the whole file, so it's stable and append-only). It is the
    identity the server keys Message.turn_index on for a runner session, which is
    what makes the live stream, catch-up, and backfill idempotent against each
    other. User + assistant text only (tool blocks skipped, matching the v1
    bridge); non-conversational records advance the index without emitting a row.
    Pass since=-1 for the full transcript."""
    out: list[dict] = []
    for i, rec in enumerate(records):
        if i <= since:
            continue
        kind = rec.get("type")
        msg = rec.get("message")
        content = msg.get("content", "") if isinstance(msg, dict) else ""
        if kind == "user":
            t = _user_text(content)
            if t:
                out.append({"index": i, "role": "user", "text": t})
        elif kind == "assistant":
            t = _assistant_text(content)
            if t:
                out.append({"index": i, "role": "assistant", "text": t})
    return out


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

    def step(self, new_records: list[dict]) -> None:
        """Consume one tick's worth of newly-appended records."""
        self.ticks += 1
        if new_records:
            self.idle_ticks = 0
        else:
            self.idle_ticks += 1
        texts = new_assistant_texts(new_records, 0)
        self.pending.extend(texts)
        self.collected.extend(texts)
        if any(hands_back_to_human(r) for r in new_records):
            self.done_reason = "end_turn"
        elif self.idle_ticks >= IDLE_TICKS:
            self.done_reason = "idle"
        elif self.ticks >= MAX_TICKS:
            self.done_reason = "max_ticks"

    @property
    def finished(self) -> bool:
        """Done AND fully delivered — undelivered text keeps the turn open so the
        next tick can retry it."""
        return bool(self.done_reason) and not self.pending

    @property
    def note(self) -> str:
        chars = len("\n\n".join(self.collected))
        if self.done_reason == "end_turn":
            return f"chat reply bridged ({chars} chars)"
        return f"chat reply bridged ({chars} chars; ended on {self.done_reason})"

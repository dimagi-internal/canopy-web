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
    """The assistant's spoken output — TEXT blocks only.

    Used by the LIVE CHAT BRIDGE, which still ships prose alone: its events go to
    the turn ledger, and the same records also reach the client (with tool rows)
    down the durable stream path, so emitting tools here too would render each
    call twice under two different message ids. `_rows_for_record` is the
    full-fidelity reader.
    """
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


# --- Transcript ordinals -----------------------------------------------------
#
# One transcript RECORD can carry several content blocks, and since tool calls
# became renderable each block is its own chat row — so the raw line ordinal is
# no longer a unique key. `turn_index` is a single integer (unique per session,
# and the sort order), so the ordinal is composite: record * STRIDE + block.
#
# Multi-block records are rare but real — 38 in 45,955 across the live fleet
# (2026-07-26), including ("text","tool_use","tool_use"), i.e. exactly the
# parallel-tool-call case this feature exists to show. Keying on the record
# alone would drop every block after the first, which is the interesting half.
#
# STRIDE is a hard ceiling on blocks-per-record, so it is set far above any
# plausible fan-out (observed max: 3) rather than snugly. Overflow clamps to the
# last slot rather than bleeding into the next record's space — a collision
# inside one record loses a block; a collision ACROSS records would interleave
# two records' rows and corrupt the ordering the client pages on.
BLOCK_STRIDE = 64


def compose_index(record: int, block: int = 0) -> int:
    """The composite transcript ordinal for block `block` of record `record`."""
    return record * BLOCK_STRIDE + min(block, BLOCK_STRIDE - 1)


def end_index(record_count: int) -> int:
    """The highest ordinal a transcript of `record_count` records can hold — the
    "stream forward only, no history" marker for a first attach with no server
    marker to resume from."""
    return compose_index(max(record_count - 1, 0), BLOCK_STRIDE - 1)


# Tool payloads are the one part of a transcript that is routinely enormous: a
# Read of a large file, a Write's full body, a Bash dump. They flow to a phone
# over a websocket and into a JSONField, so they are capped HERE — at the
# producer, before the wire — rather than anywhere downstream.
TOOL_TEXT_MAX = 8_000       # a tool RESULT body
TOOL_INPUT_STR_MAX = 4_000  # any single string inside a tool's input
TOOL_INPUT_JSON_MAX = 16_000


def _truncate(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    return text[:limit] + f"\n… [truncated {len(text) - limit} chars]"


def _truncate_input(value, depth: int = 0):
    """Recursively cap the string leaves of a tool input. Structure is preserved
    (the UI renders the input as JSON, and a shape with elided values still tells
    you what the call did); only the bulk goes."""
    if depth > 6:
        return "…"
    if isinstance(value, str):
        return _truncate(value, TOOL_INPUT_STR_MAX)
    if isinstance(value, dict):
        return {k: _truncate_input(v, depth + 1) for k, v in value.items()}
    if isinstance(value, list):
        return [_truncate_input(v, depth + 1) for v in value]
    return value


def _tool_input(raw) -> dict:
    """A tool_use's input, capped. A pathological input (many large strings, each
    individually under the leaf cap) degrades to a preview rather than shipping
    megabytes: better a legible summary than a row nothing will render."""
    trimmed = _truncate_input(raw if isinstance(raw, dict) else {"value": raw})
    try:
        encoded = json.dumps(trimmed)
    except (TypeError, ValueError):
        return {"_unserializable": True}
    if len(encoded) > TOOL_INPUT_JSON_MAX:
        return {"_truncated": True, "preview": encoded[:2_000]}
    return trimmed


def _tool_result_text(content) -> str:
    """A tool_result's body as display text.

    The content is a bare string ~70% of the time and a block list otherwise
    (text, image, tool_reference — all three occur live). Non-text blocks become
    a short marker so the row still renders and, more importantly, still PAIRS:
    an image-only result that emitted nothing would leave its tool_use stuck
    showing "running…" forever.
    """
    if isinstance(content, str):
        return _truncate(content.strip(), TOOL_TEXT_MAX)
    if not isinstance(content, list):
        return ""
    parts: list[str] = []
    for b in content:
        if not isinstance(b, dict):
            continue
        if b.get("type") == "text":
            parts.append(b.get("text", ""))
        elif b.get("type") == "tool_reference":
            parts.append(f"[tool_reference: {b.get('tool_name', '')}]")
        else:
            parts.append(f"[{b.get('type', 'block')}]")
    return _truncate("".join(parts).strip(), TOOL_TEXT_MAX)


def _rows_for_record(rec: dict) -> list[dict]:
    """The chat rows one transcript record contributes, as
    [{"block","role","text","content"}] in block order.

    `content` is the row's STRUCTURED fields (empty for plain text) — the
    tool-call identity the UI pairs and renders on. `text` is its plaintext.
    """
    kind = rec.get("type")
    if kind not in ("user", "assistant"):
        return []
    msg = rec.get("message")
    content = msg.get("content", "") if isinstance(msg, dict) else ""

    # A bare-string content is always a single plain message at block 0.
    if not isinstance(content, list):
        text = _user_text(content) if kind == "user" else _assistant_text(content)
        return [{"block": 0, "role": kind, "text": text, "content": {}}] if text else []

    # Overflow can only happen if a record ever carries more blocks than the
    # stride allows (it never has — observed max is 3). Reserve the last slot for
    # a marker rather than letting the extras pile onto one ordinal, where
    # get_or_create would keep the first and drop the rest with no trace.
    if len(content) > BLOCK_STRIDE:
        kept, dropped = content[: BLOCK_STRIDE - 1], len(content) - (BLOCK_STRIDE - 1)
        rows = _rows_for_blocks(kind, kept)
        rows.append({
            "block": BLOCK_STRIDE - 1, "role": kind,
            "text": f"[{dropped} further blocks in this record were not recorded]",
            "content": {"_overflow": dropped},
        })
        return rows
    return _rows_for_blocks(kind, content)


def _rows_for_blocks(kind: str, content: list) -> list[dict]:
    rows: list[dict] = []
    for b_i, block in enumerate(content):
        if not isinstance(block, dict):
            continue
        btype = block.get("type")
        if btype == "text":
            text = str(block.get("text", "")).strip()
            if text:
                rows.append({"block": b_i, "role": kind, "text": text, "content": {}})
        elif btype == "tool_use":
            rows.append({
                "block": b_i, "role": "tool_use", "text": "",
                "content": {
                    "id": block.get("id", ""),
                    "name": block.get("name", ""),
                    "input": _tool_input(block.get("input")),
                },
            })
        elif btype == "tool_result":
            rows.append({
                "block": b_i, "role": "tool_result",
                "text": _tool_result_text(block.get("content")),
                "content": {
                    "tool_use_id": block.get("tool_use_id", ""),
                    "is_error": bool(block.get("is_error", False)),
                },
            })
        # thinking / image / anything else: not a chat row (yet). It still
        # occupies its block ordinal, so adding one later re-keys nothing.
    return rows


def conversational_messages(
    records: list[dict], since: int, *, record_offset: int = 0
) -> list[dict]:
    """Conversational rows after `since`, chronological, as
    {"index","role","text","content"}.

    `index` is the composite transcript ordinal (see `compose_index`) — stable,
    append-only, and derived purely from position in the file. It is the identity
    the server keys Message.turn_index on for a runner session, which is what
    makes the live stream, catch-up, and backfill idempotent against each other.

    Emits user/assistant text plus tool_use/tool_result rows; other block types
    and non-conversational records consume their ordinal without emitting.
    `record_offset` shifts the record ordinal for a caller reading an incremental
    batch (its records start partway through the file). Pass since=-1 for
    everything.
    """
    out: list[dict] = []
    for i, rec in enumerate(records):
        for row in _rows_for_record(rec):
            index = compose_index(i + record_offset, row["block"])
            if index <= since:
                continue
            out.append({
                "index": index, "role": row["role"],
                "text": row["text"], "content": row["content"],
            })
    return out


def row_payload(row: dict) -> dict:
    """The wire/stored payload for a row: its structured content plus its text.

    ONE shape for every hop — the live WS frame's `block`, the persisted
    Message.content, and the backfill payload are all this dict, so the client
    reads the same keys whether a row arrived live or was loaded from history.
    """
    return {**(row.get("content") or {}), "text": row.get("text", "")}


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

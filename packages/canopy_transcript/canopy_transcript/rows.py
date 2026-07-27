"""Transcript records -> chat rows, with composite ordinals and payload caps.

The one place that knows how a Claude Code content block becomes a row canopy
can store and render. Both runners call this; before it existed they had
separate implementations that had already drifted apart in fidelity.
"""
from __future__ import annotations

import json


def user_text(content) -> str:
    """A user record's text - a bare string, or the text blocks of a content list."""
    if isinstance(content, str):
        return content.strip()
    if isinstance(content, list):
        return "".join(
            b.get("text", "") for b in content
            if isinstance(b, dict) and b.get("type") == "text"
        ).strip()
    return ""


def assistant_text(content) -> str:
    """An assistant record's spoken output - TEXT blocks only.

    Used by the live chat bridge, which ships prose alone; `rows_for_record` is
    the full-fidelity reader that also emits tool rows.
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


def scrub(text: str) -> str:
    """Drop NUL bytes. Postgres rejects them outright in text and jsonb columns.

    A tool result is raw bytes from whatever the tool touched, so `Read` on a
    compressed or binary file puts one straight into the stream (found on labs
    2026-07-26, one row in 683). The write is a single transaction, so ONE such
    row 500s the whole batch — the session's history never rebuilds and the
    runner retries it every tick forever. Only NUL is stripped: every other
    control character is legal in Postgres text and is real transcript content.
    """
    return text.replace("\x00", "") if "\x00" in text else text


def _truncate(text: str, limit: int) -> str:
    text = scrub(text)
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
        return _truncate(value, TOOL_INPUT_STR_MAX)  # _truncate scrubs NUL
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


def rows_for_record(rec: dict) -> list[dict]:
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
        text = scrub(user_text(content) if kind == "user" else assistant_text(content))
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
            text = scrub(str(block.get("text", "")).strip())
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
        for row in rows_for_record(rec):
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

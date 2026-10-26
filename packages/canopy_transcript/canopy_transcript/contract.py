"""The transcript row contract: what any agent CLI's producer ships, and what the
server stores.

A runner turns its CLI's own durable log (Claude Code's `.jsonl`; a future Codex
thread's items) into ROWS and ships them; the server writes them as `Message`
rows through one funnel (`canopy_sessions.transcript_ingest.persist_transcript_rows`)
without knowing which CLI produced them. This module is that boundary written
down, in the one package both sides already share: the server validates every
incoming row with `row_problems`, and every producer's conformance test checks
its output with `producer_problems` (packages/canopy_transcript/tests/
test_contract.py, runner/canopy_acp/tests/test_contract_conformance.py).

A ROW (what a producer builds — `rows.conversational_messages`, `hooks.rows_for_hook`,
`canopy_acp.ToolCall.rows`):

    index       int     The transcript ordinal (below), or NO_ORDINAL (-1).
    role        str     One of ROLES.
    text        str     The row's plaintext ("" for a tool_use).
    content     dict    Structured fields, by role (below). May be {}.
    cwd         str     Optional: the working directory the CLI recorded.
    git_branch  str     Optional: the branch the CLI recorded.

ORDINALS. `index = compose_index(unit, block)` = `unit * BLOCK_STRIDE + block`,
where `unit` is the position of a record in the CLI's own append-only log (a
Claude `.jsonl` line, counting every line, conversational or not) and `block` the
position of a content block inside it (< BLOCK_STRIDE). It is unique per
transcript, the sort order, and the identity the server keys `Message.turn_index`
on, so re-shipping a row (retry, catch-up, backfill overlap) is a no-op. It must
be derived from POSITION ONLY: the same record yields the same ordinal on every
read, and a unit that yields no row still consumes its ordinal. `NO_ORDINAL`
marks a LIVE overlay row (a hook or ACP update: shown to watchers, never
persisted); a negative index on the durable path means an old runner, which the
server numbers itself.

ROLES and their `content`:

    user         record: dict (provenance flags, `rows.record_flags`; present on
                 every row a current producer ships, {} for a typed prompt).
    assistant    api_error: str and signed_out: bool (the CLI's own error notice).
    tool_use     id: str, name: str, input: dict (capped at the producer);
                 status: "pending" | "complete" on a live overlay row only.
    tool_result  tool_use_id: str, is_error: bool; resource_links: [{type, uri,
                 name, mimeType}] when the raw result named any.
    system       nothing yet (accepted, emitted by no producer today).

The LAST row of one model call (an assistant or a tool_use row) carries `usage:
{context_tokens: int, model: str}`: how full the context window was for it
(`rows.record_window`). Any role may carry `_overflow: int` (the marker row for
blocks past the stride),
and a stream payload carries `text` inside `content` too (`rows.row_payload`).
Unknown keys are allowed: a producer may add a field before every reader uses it.

THE WIRE. Rows travel in two envelopes, both built from `rows.row_payload`:
a `session-stream` EVENT `{kind: role, seq, index, payload: content + text, cwd?,
git_branch?}` (`rows.stream_event`), and a `session-backfill` MESSAGE `{role, text,
index, content, cwd?, git_branch?}`. `event_as_row` maps the first back to a row.

Not rows, and outside this contract: live-only stream kinds (`activity:*`,
`stop:*`), which carry index -1 and are never persisted.

Stdlib only, like the rest of the package.
"""
from __future__ import annotations

from typing import Any, Literal, TypedDict

from .rows import BLOCK_STRIDE, compose_index

USER, ASSISTANT, TOOL_USE, TOOL_RESULT, SYSTEM = (
    "user", "assistant", "tool_use", "tool_result", "system")
#: Every role a transcript row may have — the server's `Message.ROLE_CHOICES`.
ROLES = frozenset({USER, ASSISTANT, TOOL_USE, TOOL_RESULT, SYSTEM})

#: The ordinal of a live overlay row: fanned out to watchers, never persisted.
NO_ORDINAL = -1
#: The highest ordinal the server can store (`Message.turn_index` is a Postgres
#: integer). One row above it would fail its whole batch's insert.
ORDINAL_MAX = 2**31 - 1

#: The statuses a live tool_use row may carry (`hooks.STATUS_*`).
TOOL_STATUSES = frozenset({"pending", "complete"})

__all__ = [
    "ASSISTANT", "BLOCK_STRIDE", "NO_ORDINAL", "ORDINAL_MAX", "ROLES", "SYSTEM",
    "TOOL_RESULT", "TOOL_STATUSES", "TOOL_USE", "USER", "TranscriptRow",
    "compose_index", "event_as_row", "producer_problems", "row_problems",
]


# --- the shapes, for readers and type checkers --------------------------------

class ResourceLink(TypedDict):
    type: Literal["resource_link"]
    uri: str
    name: str
    mimeType: str


class ContextUsage(TypedDict):
    context_tokens: int
    model: str


class ToolUseContent(TypedDict, total=False):
    id: str
    name: str
    input: dict[str, Any]
    status: Literal["pending", "complete"]


class ToolResultContent(TypedDict, total=False):
    tool_use_id: str
    is_error: bool
    resource_links: list[ResourceLink]


class TranscriptRow(TypedDict, total=False):
    index: int
    role: Literal["user", "assistant", "tool_use", "tool_result", "system"]
    text: str
    content: dict[str, Any]
    cwd: str
    git_branch: str


# --- validation -----------------------------------------------------------------

def _is_int(value) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _typed(content: dict, key: str, kind, problems: list[str], what: str) -> None:
    """A present `content[key]` has the right type (an absent one is fine here)."""
    if key not in content:
        return
    value = content[key]
    ok = _is_int(value) if kind is int else isinstance(value, kind)
    if not ok:
        problems.append(f"content.{key} must be {what}")


def row_problems(row) -> list[str]:
    """What is wrong with `row` as the server receives it; [] for a row it can store.

    The server's bar, so it is the tolerant one: `content` may be missing (treated
    as {}), and a role's fields are checked only for their TYPE when present — a
    row an old runner shipped and the server always stored still passes. What fails
    is what would corrupt storage or rendering: an unknown role, a non-integer or
    unstorable ordinal, non-string text, a mistyped structured field."""
    if not isinstance(row, dict):
        return ["row must be an object"]
    problems: list[str] = []
    role = row.get("role")
    if role not in ROLES:
        problems.append(f"role must be one of {sorted(ROLES)}, not {role!r}")
    index = row.get("index")
    if index is not None:
        if not _is_int(index):
            problems.append("index must be an integer")
        elif index > ORDINAL_MAX:
            problems.append(f"index {index} is above ORDINAL_MAX")
    if not isinstance(row.get("text", ""), str):
        problems.append("text must be a string")
    for key in ("cwd", "git_branch"):
        if row.get(key) is not None and not isinstance(row[key], str):
            problems.append(f"{key} must be a string")
    content = row.get("content", {})
    if content is None:
        content = {}
    if not isinstance(content, dict):
        problems.append("content must be an object")
        return problems
    _typed(content, "text", str, problems, "a string")
    _typed(content, "_overflow", int, problems, "an integer")
    _typed(content, "usage", dict, problems, "an object")
    usage = content.get("usage")
    if isinstance(usage, dict):
        if not _is_int(usage.get("context_tokens")):
            problems.append("content.usage.context_tokens must be an integer")
        if not isinstance(usage.get("model", ""), str):
            problems.append("content.usage.model must be a string")
    if role == USER:
        _typed(content, "record", dict, problems, "an object")
    elif role == ASSISTANT:
        _typed(content, "api_error", str, problems, "a string")
        _typed(content, "signed_out", bool, problems, "a boolean")
    elif role == TOOL_USE:
        _typed(content, "id", str, problems, "a string")
        _typed(content, "name", str, problems, "a string")
        _typed(content, "input", dict, problems, "an object")
        if "status" in content and content["status"] not in TOOL_STATUSES:
            problems.append(f"content.status must be one of {sorted(TOOL_STATUSES)}")
    elif role == TOOL_RESULT:
        _typed(content, "tool_use_id", str, problems, "a string")
        _typed(content, "is_error", bool, problems, "a boolean")
        _typed(content, "resource_links", list, problems, "a list")
        for link in content.get("resource_links") or ():
            if not (isinstance(link, dict) and link.get("type") == "resource_link"
                    and all(isinstance(link.get(k), str) and link.get(k)
                            for k in ("uri", "name", "mimeType"))):
                problems.append("content.resource_links holds a malformed link")
                break
    return problems


#: The fields every producer must put on a row of each role (`producer_problems`).
_REQUIRED = {
    USER: (("record", dict),),
    TOOL_USE: (("id", str), ("name", str), ("input", dict)),
    TOOL_RESULT: (("tool_use_id", str), ("is_error", bool)),
}


def producer_problems(row, *, live: bool = False) -> list[str]:
    """What is wrong with `row` as a CURRENT producer must build it — the bar every
    CLI's mapper is held to by its conformance test, stricter than the server's.

    On top of `row_problems`: every key present (`index`, `role`, `text`,
    `content`), the role's required fields present, a tool call's id non-empty
    (without it the call and its result never pair), and the ordinal on the right
    side of the line — `live=True` for an overlay row (`NO_ORDINAL`), else a real
    ordinal (>= 0). A `status` belongs on live rows only."""
    problems = row_problems(row)
    if not isinstance(row, dict):
        return problems
    for key in ("index", "role", "text", "content"):
        if key not in row:
            problems.append(f"{key} is missing")
    index = row.get("index")
    if _is_int(index):
        if live and index != NO_ORDINAL:
            problems.append(f"a live row's index must be {NO_ORDINAL}")
        if not live and index < 0:
            problems.append("a durable row's index must be >= 0")
    content = row.get("content") if isinstance(row.get("content"), dict) else {}
    for key, _kind in _REQUIRED.get(row.get("role"), ()):
        if key not in content:
            problems.append(f"content.{key} is missing")
    if row.get("role") == TOOL_USE and not content.get("id"):
        problems.append("content.id must not be empty")
    if row.get("role") == TOOL_RESULT and not content.get("tool_use_id"):
        problems.append("content.tool_use_id must not be empty")
    if not live and "status" in content:
        problems.append("content.status belongs on live rows only")
    return problems


def event_as_row(event: dict) -> dict:
    """A `session-stream` event as the row it carries — the mapping the server's
    ingest applies (`apps/harness/session_sync._ingest`)."""
    payload = event.get("payload")
    payload = payload if isinstance(payload, dict) else {}
    row = {"index": event.get("index", NO_ORDINAL), "role": event.get("kind"),
           "text": payload.get("text", ""), "content": payload}
    for key in ("cwd", "git_branch"):
        if event.get(key):
            row[key] = event[key]
    return row

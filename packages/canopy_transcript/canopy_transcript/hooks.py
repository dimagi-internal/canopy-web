"""Claude Code hook payloads -> the same row shape the transcript produces.

Hooks are the *live* half of canopy's session record. The transcript is complete
but lags — the Claude Code docs are explicit that it "may lag the in-memory
conversation" — so it can't drive a view you're actively watching. Hooks push
the same content the moment it happens, on a documented schema, for both
emdash-driven and `claude -p` sessions.

The two surfaces reconcile because they share one key: `tool_use_id`. A hook
passes it directly; the transcript carries it as `tool_use.id` /
`tool_result.tool_use_id`. That is what lets a live row be *replaced* by its
durable row rather than duplicated.

One thing hooks do better than the transcript: **`PostToolUse` carries the
result as well as the input**, so a single event is a complete tool_use +
tool_result pair. The transcript splits those across two records.

Live rows carry NO ordinal (`index = -1`). That is deliberate and load-bearing:
the server persists only ordinal-keyed rows, so a hook event fans out to
watching clients and is never written. The durable record stays exactly one
thing — the transcript — and the hook path is therefore allowed to drop events.
"""
from __future__ import annotations

from .rows import row_payload, scrub, _tool_input, _tool_result_text

# The hook events worth forwarding. PreToolUse is deliberately absent: it can
# BLOCK a tool call, and nothing about observability should be able to stall an
# agent. PostToolUse alone already carries input and result together.
FORWARDED_EVENTS = ("PostToolUse", "PostToolUseFailure")


def _result_text(response) -> str:
    """A hook's `tool_response` as display text.

    Bash-family tools return a dict (`stdout`/`stderr`/…); others return a bare
    string or a block list, which is the transcript's own shape and so is read
    with the transcript's own reader.
    """
    if isinstance(response, dict):
        for key in ("stdout", "content", "text", "result"):
            value = response.get(key)
            if isinstance(value, str) and value.strip():
                return _tool_result_text(value)
        err = response.get("stderr")
        if isinstance(err, str) and err.strip():
            return _tool_result_text(err)
        return ""
    return _tool_result_text(response)


def _is_error(payload: dict) -> bool:
    if payload.get("hook_event_name") == "PostToolUseFailure":
        return True
    response = payload.get("tool_response")
    return bool(isinstance(response, dict) and response.get("is_error"))


def rows_for_hook(payload: dict) -> list[dict]:
    """The live chat rows one hook event contributes.

    Returns the tool_use and its tool_result as a pair, since PostToolUse
    carries both. `index` is -1 on every row: live events are a view overlay and
    must never be persisted (see the module docstring).

    Returns [] for any event that isn't a forwarded tool event, or that carries
    no `tool_use_id` — without that key a row cannot be reconciled against its
    durable counterpart, and an unreconcilable row would duplicate forever.
    """
    if payload.get("hook_event_name") not in FORWARDED_EVENTS:
        return []
    tool_use_id = payload.get("tool_use_id")
    if not isinstance(tool_use_id, str) or not tool_use_id:
        return []
    name = scrub(str(payload.get("tool_name") or ""))
    return [
        {
            "index": -1, "role": "tool_use", "text": "",
            "content": {
                "id": tool_use_id,
                "name": name,
                "input": _tool_input(payload.get("tool_input")),
            },
        },
        {
            "index": -1, "role": "tool_result",
            "text": _result_text(payload.get("tool_response")),
            "content": {"tool_use_id": tool_use_id, "is_error": _is_error(payload)},
        },
    ]


def events_for_hook(payload: dict) -> list[dict]:
    """`rows_for_hook`, shaped as the wire events `/session-stream` accepts."""
    return [
        {"kind": r["role"], "seq": -1, "index": -1, "payload": row_payload(r)}
        for r in rows_for_hook(payload)
    ]

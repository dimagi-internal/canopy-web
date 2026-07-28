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

# PreToolUse fires when a call STARTS, PostToolUse when it finishes. Both are
# forwarded, which is what turns the view from "something happened" into a
# lifecycle: a row appears the instant a tool starts and fills in when it
# completes.
#
# PreToolUse was excluded at first because it CAN block a tool call. That risk
# belongs to a hook that denies or hangs; ours does neither — it is
# fire-and-forget with a hard 2s cap and never returns a decision, so it has no
# way to stall an agent. The safety argument does not survive the hook being
# unable to answer.
#
# This also happens to be the shape ACP already specifies (`tool_call` then
# `tool_call_update`, carrying a status), so it is the right model to converge
# on rather than a stopgap.
FORWARDED_EVENTS = ("PreToolUse", "PostToolUse", "PostToolUseFailure")

# Turn-boundary events. These carry no chat content — they answer "is the agent
# working RIGHT NOW", which the tool events cannot: between submitting a prompt
# and the first tool call, Claude is thinking and emits nothing at all, so the
# session read as idle for the most interesting part of a turn (reported
# 2026-07-27: "the fact that it's running is still not being shown before any
# text is output").
#
# `last_interacted_at` could not answer this either — it comes from emdash's own
# DB via the session report, so it lags a report cycle and has a 120s window.
# A hook fires the instant the turn starts.
# `Notification` fires when Claude Code wants a human: it needs permission for a
# tool, or the prompt has sat idle waiting for input. Both mean the same thing to
# a watcher — this session is BLOCKED on you — and neither was visible before:
# a session waiting on a permission dialog nobody can see reads exactly like one
# that is working, which is the worst of the three states to get wrong.
#
# It is a COARSE signal by necessity. emdash knows the difference because it is
# an ACP client and gets `session/request_permission` with a real
# `pendingPermissionCount`; a hook observer gets one notification and a message
# string. Distinguishing the two by parsing that string would be guessing, so we
# do not — "wants you" is the honest resolution of what a hook can see.
ACTIVITY_EVENTS = ("UserPromptSubmit", "Stop", "Notification")

# A pending call has no result yet. The client renders it as "running…" and
# replaces it when the matching PostToolUse (or the transcript row) lands, keyed
# on tool_use_id.
STATUS_PENDING = "pending"
STATUS_COMPLETE = "complete"


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

    PreToolUse yields the tool_use ALONE — the call has started and has no
    result yet, so the client shows it as running. PostToolUse yields the pair,
    because it carries input and result together, and its tool_use row replaces
    the pending one by `tool_use_id`.

    `index` is -1 on every row: live events are a view overlay and must never be
    persisted (see the module docstring).

    Returns [] for any event that isn't a forwarded tool event, or that carries
    no `tool_use_id` — without that key a row cannot be reconciled against its
    durable counterpart, and an unreconcilable row would duplicate forever.
    """
    event = payload.get("hook_event_name")
    if event not in FORWARDED_EVENTS:
        return []
    tool_use_id = payload.get("tool_use_id")
    if not isinstance(tool_use_id, str) or not tool_use_id:
        return []
    name = scrub(str(payload.get("tool_name") or ""))
    if event == "PreToolUse":
        # Starting: the tool_use row only. No tool_result is emitted, precisely
        # so the UI can show "running…" rather than a call that looks finished
        # with an empty result.
        return [{
            "index": -1, "role": "tool_use", "text": "",
            "content": {
                "id": tool_use_id,
                "name": name,
                "input": _tool_input(payload.get("tool_input")),
                "status": STATUS_PENDING,
            },
        }]
    return [
        {
            "index": -1, "role": "tool_use", "text": "",
            "content": {
                "id": tool_use_id,
                "name": name,
                "input": _tool_input(payload.get("tool_input")),
                "status": STATUS_COMPLETE,
            },
        },
        {
            "index": -1, "role": "tool_result",
            "text": _result_text(payload.get("tool_response")),
            "content": {"tool_use_id": tool_use_id, "is_error": _is_error(payload)},
        },
    ]


def activity_for_hook(payload: dict) -> str | None:
    """"working" / "idle" / "blocked" for a turn-boundary event, else None.

    Separate from `rows_for_hook` because this is not a chat row — it is a state
    transition, with nothing to render in the transcript and nothing to persist.
    """
    event = payload.get("hook_event_name")
    if event == "UserPromptSubmit":
        return "working"
    if event == "Stop":
        return "idle"
    if event == "Notification":
        return "blocked"
    return None


def events_for_hook(payload: dict) -> list[dict]:
    """`rows_for_hook`, shaped as the wire events `/session-stream` accepts."""
    return [
        {"kind": r["role"], "seq": -1, "index": -1, "payload": row_payload(r)}
        for r in rows_for_hook(payload)
    ]

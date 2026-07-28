"""ACP `session/update` stream -> canopy's row shape.

ACP describes what canopy hand-rolled: `tool_call` / `tool_call_update` carrying
a status, `agent_message_chunk` for reply text, `agent_thought_chunk` for
thinking, `usage_update` for tokens. This module turns that stream into the
SAME rows `canopy_transcript.rows_for_hook` produces, so the client renders one
protocol whatever produced it.

**`tool_call_update` is a sparse patch, not a row.** Measured against
claude-agent-acp 0.63.0, one `echo` produced five messages: the opener carried a
placeholder title ("Terminal") and `status: pending`, the real title arrived on
the next, the tool's output arrived on a message carrying *nothing else at all*,
and `status: completed` only on the last. Treating any single update as a
complete row renders half-empty tool calls; treating a later one as
authoritative erases the title. Hence a reducer: fields are merged, and an
absent field means "unchanged", never "cleared".

Live rows carry `index = -1`, exactly as the hook path does — the durable record
stays the transcript, which an ACP session writes normally
(`~/.claude/projects/<encoded-cwd>/<sessionId>.jsonl`, verified 2026-07-27). So
this whole module is a view concern and is allowed to drop anything.
"""
from __future__ import annotations

from canopy_transcript import STATUS_COMPLETE, STATUS_PENDING, scrub
from canopy_transcript.rows import _tool_input, _tool_result_text

# ACP status values that mean the call is over. `failed` is terminal too — the
# row is complete, the RESULT is an error, and those are different questions.
TERMINAL_STATUSES = ("completed", "failed", "cancelled")
ERROR_STATUSES = ("failed",)


class ToolCall:
    """One tool call, accumulated across its patches."""

    __slots__ = ("id", "title", "kind", "status", "tool_name", "raw_input",
                 "raw_output", "_content_text", "_tool_response")

    def __init__(self, call_id: str):
        self.id = call_id
        self.title = ""
        self.kind = ""
        self.status = ""
        self.tool_name = ""
        self.raw_input: dict = {}
        self.raw_output = None
        self._content_text = ""
        self._tool_response = None

    @property
    def is_complete(self) -> bool:
        return self.status in TERMINAL_STATUSES

    @property
    def is_error(self) -> bool:
        if self.status in ERROR_STATUSES:
            return True
        resp = self._tool_response
        return bool(isinstance(resp, dict) and resp.get("is_error"))

    @property
    def result_text(self) -> str:
        """Display text for the result.

        `_meta.claudeCode.toolResponse` is preferred because it is the same dict
        the PostToolUse hook carries (`stdout`/`stderr`/…), so a row produced
        here and a row produced from a hook read identically. ACP's rendered
        `content` is the fallback — it is markdown-fenced for display, which is
        right for a UI and wrong as the canonical text.
        """
        resp = self._tool_response
        if isinstance(resp, dict):
            for key in ("stdout", "content", "text", "result"):
                value = resp.get(key)
                if isinstance(value, str) and value.strip():
                    return _tool_result_text(value)
            err = resp.get("stderr")
            if isinstance(err, str) and err.strip():
                return _tool_result_text(err)
        if self._content_text:
            return _tool_result_text(self._content_text)
        if isinstance(self.raw_output, str):
            return _tool_result_text(self.raw_output)
        return ""

    # -- patching ----------------------------------------------------------

    def apply(self, update: dict) -> None:
        """Merge one patch. An absent key leaves the current value alone —
        that is the whole contract of this class."""
        for field in ("title", "kind", "status"):
            value = update.get(field)
            if isinstance(value, str) and value:
                setattr(self, field, value)
        raw_input = update.get("rawInput")
        # `tool_call` opens with rawInput={} before the arguments are known;
        # an empty dict must not overwrite arguments already received.
        if isinstance(raw_input, dict) and raw_input:
            self.raw_input = raw_input
        if "rawOutput" in update:
            self.raw_output = update["rawOutput"]
        meta = update.get("_meta")
        if isinstance(meta, dict):
            claude = meta.get("claudeCode")
            if isinstance(claude, dict):
                name = claude.get("toolName")
                if isinstance(name, str) and name:
                    self.tool_name = name
                if "toolResponse" in claude:
                    self._tool_response = claude["toolResponse"]
        text = _content_text(update.get("content"))
        if text:
            self._content_text = text

    def rows(self) -> list[dict]:
        """The live chat rows for this call, in `rows_for_hook`'s shape.

        A pending call yields the tool_use ALONE — the same rule PreToolUse
        follows, so the UI shows "running…" instead of a finished-looking call
        with an empty result.
        """
        use = {
            "index": -1, "role": "tool_use", "text": "",
            "content": {
                "id": self.id,
                "name": scrub(self.tool_name or self.kind or ""),
                "input": _tool_input(self.raw_input),
                "status": STATUS_COMPLETE if self.is_complete else STATUS_PENDING,
            },
        }
        if not self.is_complete:
            return [use]
        return [use, {
            "index": -1, "role": "tool_result", "text": self.result_text,
            "content": {"tool_use_id": self.id, "is_error": self.is_error},
        }]


def _content_text(content) -> str:
    """Flatten ACP's `content` blocks to text.

    Shape: `[{"type": "content", "content": {"type": "text", "text": "…"}}]`,
    with a bare `{"type": "text"}` block also seen.
    """
    if isinstance(content, dict):
        content = [content]
    if not isinstance(content, list):
        return ""
    parts = []
    for block in content:
        if not isinstance(block, dict):
            continue
        inner = block.get("content")
        if isinstance(inner, dict):
            text = inner.get("text")
        else:
            text = block.get("text")
        if isinstance(text, str) and text:
            parts.append(text)
    return "".join(parts)


class UpdateReducer:
    """Accumulates one session's `session/update` stream.

    Deliberately passive: it holds state and answers questions. The transport
    (`AcpClient`) drives it, and the runner decides what to forward — so the
    reducer is testable against recorded update sequences with no subprocess.
    """

    def __init__(self):
        self._calls: dict[str, ToolCall] = {}      # insertion-ordered by dict
        self.assistant_text = ""
        self.thinking_text = ""
        self.usage: dict = {}
        self.rate_limit: dict = {}
        self.title = ""
        self.available_commands: list = []
        self.mode = ""
        self.plan: list = []

    # -- ingest ------------------------------------------------------------

    def apply(self, update: dict) -> str | None:
        """Apply one update. Returns its kind, or None if it was ignored.

        Unknown kinds are ignored rather than fatal: ACP is versioned and the
        adapter ships new update kinds, and none of them may cost a turn.
        """
        if not isinstance(update, dict):
            return None
        kind = update.get("sessionUpdate")
        handler = _HANDLERS.get(kind)
        if handler is None:
            return None
        handler(self, update)
        return kind

    def _tool(self, update: dict) -> None:
        call_id = update.get("toolCallId")
        if not isinstance(call_id, str) or not call_id:
            return
        call = self._calls.get(call_id)
        if call is None:
            call = self._calls[call_id] = ToolCall(call_id)
        call.apply(update)

    def _agent_message(self, update: dict) -> None:
        self.assistant_text += _content_text(update.get("content"))

    def _agent_thought(self, update: dict) -> None:
        self.thinking_text += _content_text(update.get("content"))

    def _usage(self, update: dict) -> None:
        for key in ("used", "size", "cost"):
            if key in update:
                self.usage[key] = update[key]
        meta = update.get("_meta")
        if isinstance(meta, dict):
            limit = meta.get("_claude/rateLimit")
            # Most usage_updates carry no rate-limit meta. Assigning
            # unconditionally would blank the signal moments after it arrived.
            if isinstance(limit, dict) and limit:
                self.rate_limit = limit

    def _session_info(self, update: dict) -> None:
        title = update.get("title")
        if isinstance(title, str) and title:
            self.title = title

    def _commands(self, update: dict) -> None:
        commands = update.get("availableCommands")
        if isinstance(commands, list):
            self.available_commands = commands

    def _mode(self, update: dict) -> None:
        mode = update.get("currentModeId")
        if isinstance(mode, str) and mode:
            self.mode = mode

    def _plan(self, update: dict) -> None:
        entries = update.get("entries")
        if isinstance(entries, list):
            self.plan = entries

    def reset_stream_state(self) -> None:
        """Drop everything accumulated from the stream so far, keeping session
        facts (title, commands, mode, usage, rate limit).

        Exists for `session/load`, which REPLAYS the whole prior conversation as
        ordinary updates. Without a reset the resumed turn's reply text is the
        old conversation concatenated with the new one, and every historical
        tool call looks like it just ran.
        """
        self._calls.clear()
        self.assistant_text = ""
        self.thinking_text = ""

    # -- read --------------------------------------------------------------

    @property
    def tool_calls(self) -> list[ToolCall]:
        """Calls in arrival order."""
        return list(self._calls.values())

    def tool_call(self, call_id: str) -> ToolCall | None:
        return self._calls.get(call_id)

    def rows_for_tool_call(self, call_id: str) -> list[dict]:
        call = self._calls.get(call_id)
        return call.rows() if call is not None else []


_HANDLERS = {
    "tool_call": UpdateReducer._tool,
    "tool_call_update": UpdateReducer._tool,
    "agent_message_chunk": UpdateReducer._agent_message,
    "agent_thought_chunk": UpdateReducer._agent_thought,
    "usage_update": UpdateReducer._usage,
    "session_info_update": UpdateReducer._session_info,
    "available_commands_update": UpdateReducer._commands,
    "current_mode_update": UpdateReducer._mode,
    "plan": UpdateReducer._plan,
}

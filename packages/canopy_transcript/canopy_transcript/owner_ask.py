"""The one line a caller's session leaves for the agent's owner.

A confined session (a caller who is not the agent's owner or an admin) cannot
act on anything; its procedure ends the final message with
`For <Agent>'s owner: <the ask, one line>` (hal's `answer-caller`, ace's `ask`).
The transcript holding that line is the caller's conversation, and nobody
reviews it, so the runner lifts the line out and canopy-web puts it in the
turn's `result_note`, the part of a turn the owner reads (canopy-web#1400 item 5,
#1416). This module finds it; it reads nothing else and judges nothing.
"""
from __future__ import annotations

import re

from .records import read_tail_records

#: `For Hal's owner: <ask>`, alone on its line, tolerating markdown dressing
#: (`**For Hal's owner:** …`, `> For …`) and a curly apostrophe.
_LINE = re.compile(
    r"^[ \t>*_`-]*For[ \t]+([^\n'’*_`]{1,80}?)['’]s[ \t]+owner:[ \t*_`]*(\S[^\n]*?)[ \t*_`]*$",
    re.MULTILINE | re.IGNORECASE)

#: The longest line passed on. One line is the contract; this is a backstop.
MAX_CHARS = 500


def find(text) -> str | None:
    """The LAST owner line in `text`, normalised, or None."""
    if not isinstance(text, str) or not text:
        return None
    found = None
    for m in _LINE.finditer(text):
        found = m
    if found is None:
        return None
    name, ask = found.group(1).strip(), found.group(2).strip()
    return f"For {name}'s owner: {ask}"[:MAX_CHARS] if name and ask else None


def final_text(records: list[dict]) -> str:
    """The text of the session's final answer: the assistant text written after
    the last user record (a prompt or a tool result) in `records`."""
    parts: list[str] = []
    for record in reversed(records):
        kind = record.get("type")
        if kind in ("system", "summary", "queue-operation") or record.get("isSidechain"):
            continue
        if kind == "user":
            break
        if kind != "assistant":
            continue
        content = (record.get("message") or {}).get("content")
        if isinstance(content, str):
            parts.append(content)
        elif isinstance(content, list):
            parts.extend(b["text"] for b in reversed(content)
                         if isinstance(b, dict) and b.get("type") == "text"
                         and isinstance(b.get("text"), str))
    return "\n".join(reversed(parts))


def from_stop_hook(payload: dict) -> str | None:
    """The owner line of the turn a `Stop` hook just ended, or None. Prefers the
    hook's own `last_assistant_message`, which newer Claude Code sends, and
    falls back to the transcript's tail."""
    line = find(payload.get("last_assistant_message"))
    if line:
        return line
    path = payload.get("transcript_path")
    if not isinstance(path, str) or not path:
        return None
    return find(final_text(read_tail_records(path)))

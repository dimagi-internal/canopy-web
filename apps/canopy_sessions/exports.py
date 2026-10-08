"""Session export — the session as the web view shows it, handed to the person who
started it as readable markdown for their own Claude to pick the work up.

The source is the one the chat page reads: the session's `Message` rows, which
the runner produces from its transcript (`canopy_transcript.conversational_messages`)
and streams to canopy-web. Never the runner's raw `.jsonl` — the goal is that
their Claude can READ where things stand, and nothing here asks a runner for
anything.
"""
from __future__ import annotations

import datetime as dt
import json

from .models import Message, Session

#: A tool's input or output past this is shortened — the web view collapses them
#: too; the reader needs what was done, not every byte it printed.
TOOL_INPUT_CHARS = 300
TOOL_RESULT_CHARS = 1500


def build_markdown(session: Session) -> tuple[str, int]:
    """(markdown, message_count) — the session's rows, in the web view's order."""
    rows = list(session.messages.order_by("turn_index"))
    who = session.agent.slug if session.agent_id else "Agent"
    blocks = [b for b in (_render(m, who) for m in rows) if b]
    if not blocks:
        return "", 0
    where = session.project or (session.agent.slug if session.agent_id else "")
    head = [
        f"# {session.title or 'canopy session'}",
        "",
        f"Exported from canopy session `{session.id}`"
        + (f" ({where})" if where else "") + f" on {_day(dt.datetime.now(dt.timezone.utc))}.",
        "",
        "> This is the session as canopy's web view shows it. Tool output is "
        "shortened, and the work was done on the canopy runner — check git (branch, "
        "open PRs, recent commits) for where things actually stand before continuing.",
        "",
    ]
    return "\n".join(head + blocks), len(blocks)


def _render(m: Message, agent: str) -> str:
    text = (m.plaintext or "").strip()
    stamp = _day(m.created_at)
    if m.role == Message.USER and text:
        return f"## {_speaker(m)} · {stamp}\n\n{text}\n"
    if m.role == Message.ASSISTANT and text:
        return f"## {agent} · {stamp}\n\n{text}\n"
    if m.role == Message.TOOL_USE:
        c = m.content or {}
        args = json.dumps(c.get("input") or {}, ensure_ascii=False)
        return f"→ `{c.get('name') or 'tool'}` {_cut(args, TOOL_INPUT_CHARS)}\n"
    if m.role == Message.TOOL_RESULT and text:
        fence = "````" if "```" in text else "```"
        return f"{fence}\n{_cut(text, TOOL_RESULT_CHARS)}\n{fence}\n"
    if m.role == Message.SYSTEM and text:
        return f"_{text}_\n"
    return ""


def _cut(s: str, n: int) -> str:
    return s if len(s) <= n else s[: n - 1] + "…"


def _speaker(m: Message) -> str:
    author = m.author or {}
    return author.get("name") or author.get("email") or "You"


def _day(when: dt.datetime) -> str:
    return when.astimezone(dt.timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

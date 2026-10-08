"""Session export — the conversation, handed to the person who started it, as a
readable markdown transcript they can give their own Claude to pick the work up.

Built from what canopy already holds and the human already saw: the session's
`Message` rows, user and assistant text only. Never the runner's raw `.jsonl` —
the goal is that their Claude can READ where things stand, not replay the
session, and nothing here asks a runner for anything.
"""
from __future__ import annotations

import datetime as dt

from .models import Message, Session

#: What the person saw in the conversation. Tool rows are the agent's machinery
#: (the widget hides them too — `services.WIDGET_HIDDEN_ROLES`); system rows are
#: canopy's own notices.
EXPORT_ROLES = (Message.USER, Message.ASSISTANT)


def build_markdown(session: Session) -> tuple[str, int]:
    """(markdown, message_count) — the conversation as the person saw it."""
    rows = list(session.messages.filter(role__in=EXPORT_ROLES)
                .exclude(plaintext="").order_by("turn_index"))
    rows = [m for m in rows if m.plaintext.strip()]
    if not rows:
        return "", 0
    who = session.agent.slug if session.agent_id else "Agent"
    where = session.project or (session.agent.slug if session.agent_id else "")
    out = [
        f"# {session.title or 'canopy session'}",
        "",
        f"Exported from canopy session `{session.id}`"
        + (f" ({where})" if where else "") + f" on {_day(dt.datetime.now(dt.timezone.utc))}.",
        "",
        # Said up front: a reader seeing replies with no tool calls behind them
        # starts treating the described work as unverified (observed on the first
        # export read back, 2026-10-08). The work is real — it is on the runner.
        "> Only the conversation is here — the agent's tool calls and their output "
        "are not. Work it describes was done on the canopy runner, so check git "
        "(branch, open PRs, recent commits) for where things actually stand before "
        "continuing.",
        "",
    ]
    for m in rows:
        speaker = _speaker(m) if m.role == Message.USER else who
        out += [f"## {speaker} · {_day(m.created_at)}", "", m.plaintext.strip(), ""]
    return "\n".join(out), len(rows)


def _speaker(m: Message) -> str:
    author = m.author or {}
    return author.get("name") or author.get("email") or "You"


def _day(when: dt.datetime) -> str:
    return when.astimezone(dt.timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

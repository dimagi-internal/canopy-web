"""Who said a line, carried THROUGH Claude's transcript.

A transcript-sourced session's durable user rows are re-read from Claude's own
transcript, which records the prompt the agent received and nothing else — so
an author known only to canopy's database is lost the moment the row comes back
(spec 2026-09-26). The fix is to put the author where the transcript will keep
it: one marker line at the top of the delivered prompt. `mark` writes it (at
claim, never into `Turn.prompt` — see the spec for the readers that must not see
it), `parse` reads it back on every durable and live path.

Strict on purpose: only an exact FIRST line counts, so a person or agent quoting
the syntax mid-message is never misattributed.
"""
from __future__ import annotations

import re
import uuid

_MARKER = re.compile(
    r'^\[canopy from="(?P<name>(?:[^"\\]|\\.)*)" '
    r'(?:user=(?P<user>\d+)|contact=(?P<contact>\d+)) '
    r'turn=(?P<turn>[0-9a-f]{32})\]$'
)


def _escape(name: str) -> str:
    one_line = " ".join(name.split())
    return one_line.replace("\\", "\\\\").replace('"', '\\"')


def _unescape(name: str) -> str:
    return re.sub(r"\\(.)", r"\1", name)


def mark(text: str, *, name: str, turn_id, user_id: int | None = None,
         contact_id: int | None = None) -> str:
    if (user_id is None) == (contact_id is None):
        raise ValueError("exactly one of user_id / contact_id")
    who = f"user={int(user_id)}" if user_id is not None else f"contact={int(contact_id)}"
    tid = uuid.UUID(str(turn_id)).hex
    return f'[canopy from="{_escape(name)}" {who} turn={tid}]\n{text}'


def parse(text: str) -> tuple[dict | None, str, str | None]:
    first, sep, rest = text.partition("\n")
    m = _MARKER.match(first)
    if m is None:
        return None, text, None
    author: dict = {"name": _unescape(m["name"])}
    if m["user"] is not None:
        author["user_id"] = int(m["user"])
    else:
        author["contact_id"] = int(m["contact"])
    return author, rest if sep else "", m["turn"]


def _display_name(user) -> str:
    return (user.get_full_name() or "").strip() or user.email


def for_turn(turn) -> str:
    """The prompt as the runner should deliver it. Only a chat-session turn with
    a known person is marked; everything else (agent, scheduled, email turns)
    is delivered exactly as before."""
    prompt = turn.prompt or ""
    if not turn.chat_session_id:
        return prompt
    if turn.initiator_user_id:
        return mark(prompt, name=_display_name(turn.initiator_user),
                    user_id=turn.initiator_user_id, turn_id=turn.pk)
    if turn.initiator_contact_id:
        contact = turn.initiator_contact
        name = (getattr(contact, "display_name", "") or getattr(contact, "email", "") or "contact").strip()
        return mark(prompt, name=name, contact_id=turn.initiator_contact_id, turn_id=turn.pk)
    return prompt

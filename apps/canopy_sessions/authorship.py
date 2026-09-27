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


# The sources a PERSON types into a chat from. Everything else that lands on a
# chat session is a program speaking: an email turn (bound to its thread's
# session by `email_thread_session`, prompt `/echo:turn --thread …`), a
# scheduled turn, an MCP/API caller, a transfer preamble.
_CHAT_ORIGINS = frozenset({"canopy_web_chat", "slack", "ace_web"})
# `send_message` keys every send `chat:<session>:<client_id|index|nonce>`; a
# transfer (`transfer:…`) or any other enqueue on the session does not.
_SEND_KEY_PREFIX = "chat:"


def is_chat_send(turn) -> bool:
    """A person's own chat line — the only kind of turn that carries a marker,
    and the only kind the queued list shows."""
    if not turn.chat_session_id:
        return False
    if not str(getattr(turn, "idempotency_key", "") or "").startswith(_SEND_KEY_PREFIX):
        return False
    origin = getattr(turn, "origin", "") or ""
    if origin in _CHAT_ORIGINS:
        return True
    # A contact on an embedding host's widget may name `api` (contact_api allows
    # it); a contact is never an API program, so it is still a person typing.
    return origin == "api" and bool(turn.initiator_contact_id) and not turn.initiator_user_id


def for_turn(turn) -> str:
    """The prompt as the runner should deliver it.

    Marked only when it is a person's chat send (`is_chat_send`) with a known
    person, and never when the prompt is a slash command: Claude Code runs a
    slash command only from the FIRST line, so a marker above `/compact` turns
    it into prose — it goes bare and unattributed instead (accepted cost).
    Everything else — email, scheduled, API, transfer turns — is delivered
    exactly as it was enqueued."""
    prompt = turn.prompt or ""
    if not is_chat_send(turn) or prompt.lstrip().startswith("/"):
        return prompt
    author = author_of(turn)
    if author is None:
        return prompt
    return mark(prompt, name=author["name"], user_id=author.get("user_id"),
                contact_id=author.get("contact_id"), turn_id=turn.pk)


def author_of(turn) -> dict | None:
    """The person behind a turn, in the marker's own shape — or None."""
    if turn.initiator_user_id:
        return {"name": " ".join(_display_name(turn.initiator_user).split()),
                "user_id": turn.initiator_user_id}
    if turn.initiator_contact_id:
        contact = turn.initiator_contact
        name = (getattr(contact, "display_name", "") or getattr(contact, "email", "") or "contact").strip()
        return {"name": " ".join(name.split()), "contact_id": turn.initiator_contact_id}
    return None

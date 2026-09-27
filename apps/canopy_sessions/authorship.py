"""Who said a line, carried THROUGH Claude's transcript.

**Superseded 2026-09-27.** canopy USED to put the author on the delivered
prompt itself — one marker line canopy prepended at claim, so the answer
would still be in whatever Claude's transcript recorded. That broke on the
laptop runner, which types a prompt into emdash as ONE line: the marker's
trailing `\n` never survived, so a row arrived as
`[canopy from="…" user=N turn=…]Are you working?` — marker and body glued
together — and `parse` (which required the marker to be the WHOLE first
line) rejected it, saving the row with `author=None` and the marker visible
in the UI.

The user's call: the prompt must reach the agent EXACTLY as typed — it
already learns who is asking from canopy's `caller_context` hook (the caller
envelope), which never touches the prompt, so marking it was redundant
there. Attribution moved server-side (`services.persist_transcript_rows`
matches an unmarked user row to the earliest unlinked chat-send Turn with
the same text, in send order) — see that function's docstring.

`mark` and `for_turn` (which wrote the marker) are gone. `parse` stays,
forever: rows already recorded, and any transcript backfilled from the days
around this change, still carry the marker. It is now TOLERANT of the
lost-newline case — the marker is accepted at the very start of the text
followed by an OPTIONAL `\n` — while the mid-text rule is unchanged: only the
very start of the text counts, so a person or agent quoting the syntax
mid-message is never misattributed.
"""
from __future__ import annotations

import re

_MARKER = re.compile(
    r'^\[canopy from="(?P<name>(?:[^"\\]|\\.)*)" '
    r'(?:user=(?P<user>\d+)|contact=(?P<contact>\d+)) '
    r'turn=(?P<turn>[0-9a-f]{32})\]\n?'
)


def _unescape(name: str) -> str:
    return re.sub(r"\\(.)", r"\1", name)


def parse(text: str) -> tuple[dict | None, str, str | None]:
    m = _MARKER.match(text)
    if m is None:
        return None, text, None
    author: dict = {"name": _unescape(m["name"])}
    if m["user"] is not None:
        author["user_id"] = int(m["user"])
    else:
        author["contact_id"] = int(m["contact"])
    return author, text[m.end():], m["turn"]


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

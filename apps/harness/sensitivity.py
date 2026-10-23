"""Whether a conversation holds a host's sensitive data — THE predicate every storage
and re-exposure rule asks (spec 2026-10-22-sensitive-data-agents §4).

A session or turn is SENSITIVE when what it requires of its runner includes
`sensitive_data` (`runner_requirements`): a host that serves real data requires
sensitive-data runners for it, and the requirement is never dropped down a chain (§3). A requirement canopy cannot
read counts as sensitive, as it counts as unsatisfiable for routing.

What follows from it, one test per surface (`tests/test_sensitive_storage.py`):

* **store less** — no `TurnTranscript`; a `tool_end` event and a `TOOL_RESULT`
  message keep the call id and `is_error`, never the result (`strip_result`);
* **forced retention** — `retention.services.sweep_sensitive`, whatever
  `CANOPY_RETENTION_ENFORCE` says;
* **refuse re-exposure** — share links, Slack share and mirroring, export, MCP Apps
  receipts and view context, the usage scan's and autotitle's title;
* **refuse re-processing** — auto-debug, and the fleet's transcript readers (there
  is no transcript to read);
* **read back by its own people only** — the words are kept SEALED on the message
  (`seal`), and only the conversation's people, on its chat surfaces, read them
  (`reads_content`, `unseal`): never a review, a search, a fleet reader or another AI.
"""
from __future__ import annotations

from . import runner_requirements as rr

SENSITIVE_DATA = rr.SENSITIVE_DATA
#: The refusal every re-exposure door gives, so a person reads one sentence.
REFUSAL = ("this conversation holds a host's sensitive data, so canopy does not share, export "
           "or copy it")
#: Why a door that would hand the words on refuses someone not in the conversation.
NOT_IN_IT = ("this conversation holds a host's sensitive data: only the people in it can "
             "bring someone in or copy it")
#: Result-bearing keys a tool result may carry, from every runner and the gateway.
_RESULT_KEYS = ("content", "text", "output", "result", "structured", "structuredContent")
#: Event kinds whose payload is a tool's answer.
RESULT_KINDS = frozenset({"tool_end", "tool_result"})


def _is(reqs) -> bool:
    return SENSITIVE_DATA in reqs or rr.UNSATISFIABLE in reqs


def is_sensitive(obj) -> bool:
    """True for a Session or a Turn whose runner requirements include `sensitive_data`.

    A turn is read through `requirements_of` (its session, the session it was
    started from and its ancestors), a session through its own metadata. None → False.
    """
    if obj is None:
        return False
    from apps.harness.models import Turn

    if isinstance(obj, Turn):
        return _is(rr.requirements_of(obj))
    return _is(rr.requirements_of_session(obj))


def strip_result(payload: dict | None) -> dict:
    """A tool result as a sensitive conversation stores it: which call, and
    whether it failed — never what came back."""
    payload = payload if isinstance(payload, dict) else {}
    out = {k: v for k, v in payload.items() if k not in _RESULT_KEYS}
    out["withheld"] = True
    return out


def any_sensitive(*objs) -> bool:
    return any(is_sensitive(o) for o in objs)


def claude_session_is_sensitive(claude_session: str) -> bool:
    """Whether a Claude session id (an uploaded transcript's, a usage report's)
    belongs to a sensitive conversation: a binding's `transcript_id`, or a turn's
    `session_key` / `cli_session_id`."""
    if not claude_session:
        return False
    from apps.canopy_sessions.models import RunnerBinding
    from apps.harness.models import Turn

    for binding in RunnerBinding.objects.select_related("session").filter(transcript_id=claude_session):
        if is_sensitive(binding.session):
            return True
    turns = Turn.objects.filter(session_key=claude_session) | Turn.objects.filter(
        cli_session_id=claude_session)
    return any(is_sensitive(t) for t in turns.select_related("chat_session", "parent_session")[:20])


# ── metadata, never content (Jonathan, 2026-10-22) ────────────────────────────
# Beyond tool results: a sensitive conversation's readable rows keep only what
# troubleshooting needs. What the person types, what the agent says and a tool's
# arguments can all carry the host's data (a name typed into a question, a case read back), so
# the stored event, every Message's columns, the prompt once the turn ends and the
# result note keep metadata only. The live frame still carries the words to the
# person watching (`ledger.append_events`), over the socket.
#
# The conversation itself is kept, sealed (below): a person can come back to it,
# and the people in it can share it (multiplayer), without anything else reading it.

import copy as _copy
import hashlib as _hashlib
import json as _json

#: What a redacted row says about itself, so a reader knows content was withheld on
#: purpose rather than lost.
REDACTED = "sensitive"
#: Payload keys that are metadata, never content. An allowlist: a key not named here
#: is dropped, so a new field a runner starts sending is withheld until someone
#: decides it is safe.
METADATA_KEYS = frozenset({
    "id", "tool_use_id", "name", "is_error", "status", "code", "exit_code",
    "duration_ms", "model", "kind", "session_id", "stop_reason", "withheld",
})
MAX_META_STRING = 200
WITHHELD_PREFIX = "[sensitive] note withheld"


def _size_and_hash(value) -> tuple[int, str]:
    raw = _json.dumps(value, sort_keys=True, default=str).encode()
    return len(raw), _hashlib.sha256(raw).hexdigest()[:16]


def redact_payload(payload) -> dict:
    """The metadata of an event payload: allowlisted scalar fields, the NAMES of a
    tool's arguments (and the host tool a gateway call reaches), and the payload's
    size and hash, so events can be matched up without being read."""
    payload = payload if isinstance(payload, dict) else {}
    out: dict = {"redacted": REDACTED}
    for key in METADATA_KEYS:
        value = payload.get(key)
        if isinstance(value, (bool, int, float)):
            out[key] = value
        elif isinstance(value, str) and value:
            out[key] = value[:MAX_META_STRING]
    tool_input = payload.get("input")
    if isinstance(tool_input, dict):
        out["input_keys"] = sorted(str(k)[:MAX_META_STRING] for k in tool_input)[:50]
        for key in ("tool", "site"):
            if isinstance(tool_input.get(key), str):
                out[f"input_{key}"] = tool_input[key][:MAX_META_STRING]
    out["bytes"], out["sha256"] = _size_and_hash(payload)
    return out


def redact_text(text: str) -> dict:
    """A string's metadata: its size and hash."""
    size, digest = _size_and_hash(text or "")
    return {"redacted": REDACTED, "bytes": size, "sha256": digest}


def withheld_note(note: str) -> str:
    """A sensitive turn's result note as canopy-web keeps it: that there was one, and
    its size. An error message can quote what the turn read."""
    if not note or note.startswith(WITHHELD_PREFIX):
        return note or ""
    return f"{WITHHELD_PREFIX} ({len(note.encode())} bytes)"


# ── sealed: the conversation, for its own people only ────────────────────────
# A sensitive conversation must still be recoverable — a reload, a second tab, a
# participant joining — so its Messages keep the words in `Message.sealed`, beside
# metadata-only columns. `sealed` is ciphertext (`apps/common/encryption.py`), so a
# database read, a backup or a shell — the doors a debugging agent would use —
# see nothing. Nothing opens it but `_open`, and nothing calls that without
# `reads_content`: the primary interaction, never a review of it.


def seal(role: str, text: str, content: dict | None) -> tuple[str, dict, str | None]:
    """(plaintext, content, sealed) as a sensitive conversation stores a Message.

    The columns are metadata. `sealed` holds the words, encrypted, except for a
    tool's result, which is never kept (`strip_result`): what a tool showed the
    person as a table or a chart is drawn again from the call's input, as the
    viewer, never from a copy."""
    from apps.common.encryption import encrypt_secret  # noqa: PLC0415 — reads settings

    content = content if isinstance(content, dict) else {}
    if role == "tool_result":
        return "", strip_result(content), None
    words = {"plaintext": text or "", "content": content}
    return "", redact_payload(content), encrypt_secret(_json.dumps(words, sort_keys=True))


def _open(sealed) -> dict | None:
    """The words a `seal` kept, or None. Private: callers go through `unseal`
    (behind `reads_content`) or `sealed_call` (no words leave it)."""
    if not sealed or not isinstance(sealed, str):
        return None
    from apps.common.encryption import decrypt_secret  # noqa: PLC0415 — reads settings

    try:
        words = _json.loads(decrypt_secret(sealed))
    except Exception:  # noqa: BLE001 — a row no key opens reads as metadata
        return None
    return words if isinstance(words, dict) else None


def sealed_call(content, sealed) -> dict:
    """A TOOL_USE row's call, for deciding server-side which View it has (its id,
    name and host tool). In a sensitive conversation the columns are metadata, so
    the call is opened here; its arguments reach a person only through
    `mcp_apps_views.resource`, behind `reads_content`."""
    words = _open(sealed)
    inner = words.get("content") if words else None
    return inner if isinstance(inner, dict) else (content if isinstance(content, dict) else {})


def is_conversation_person(session, *, user=None, contact=None) -> bool:
    """Whether this principal is IN the conversation: its contact, its creator, or a
    participant someone added (multiplayer). An agent's admin or a workspace owner
    who is not in it is not."""
    if contact is not None:
        return session.contact_id == contact.pk
    if user is None or not getattr(user, "is_authenticated", False):
        return False
    if session.created_by_id == user.pk:
        return True
    return session.participants.filter(user=user).exists()


def request_is_chat_surface(request) -> bool:
    """A chat surface: the person at a browser, or the widget / SDK (a contact's or
    a connected site's token). Never an MCP tool call, a PAT, OAuth or a session
    minted from a token — the doors through which an AI reads sessions."""
    if getattr(request, "via_mcp", False):
        return False
    if getattr(request, "contact", None) is not None or getattr(request, "delegated_app", None):
        return True
    from apps.common.views_debug import is_machine  # noqa: PLC0415 — loads the session store

    return not is_machine(request)


def scope_is_chat_surface(scope) -> bool:
    """The socket's form of `request_is_chat_surface`."""
    if scope.get("via_widget") or scope.get("contact") is not None:
        return True
    if scope.get("auth_method") != "session":
        return False
    from apps.common.views_debug import DEBUG_SESSION_MARKER  # noqa: PLC0415 — as above

    session = scope.get("session")
    try:
        return not (session is not None and session.get(DEBUG_SESSION_MARKER))
    except Exception:  # noqa: BLE001 — an unreadable session is not a person
        return False


def reads_content(session, *, chat_surface: bool, user=None, contact=None) -> bool:
    """THE gate on reading a conversation back: True for any ordinary conversation;
    for a sensitive one, only its own people on its own chat surfaces."""
    if not is_sensitive(session):
        return True
    return chat_surface and is_conversation_person(session, user=user, contact=contact)


def unseal(message):
    """A copy of `message` carrying its sealed words, for a reader `reads_content`
    admitted. A copy, so the instance a caller might save keeps its columns."""
    sealed = _open(getattr(message, "sealed", None))
    if sealed is None:
        return message
    out = _copy.copy(message)
    out.plaintext = str(sealed.get("plaintext") or "")
    out.content = {**(sealed.get("content") or {}),
                   **{k: v for k, v in (message.content or {}).items() if k == "client_id"}}
    return out


def readable(messages, session, *, chat_surface: bool, user=None, contact=None) -> list:
    """`messages` as this reader may see them: unsealed for the conversation's own
    people on its chat surfaces, the metadata columns for everyone else."""
    rows = list(messages)
    if not reads_content(session, chat_surface=chat_surface, user=user, contact=contact):
        return rows
    return [unseal(m) for m in rows]


#: How much of a sensitive conversation its next turn is handed (`history`).
HISTORY_MESSAGES = 40
HISTORY_BYTES = 24_000


def history(turn) -> list[dict] | None:
    """The conversation so far, for the next turn of a SENSITIVE conversation: the
    one reader of the sealed words besides its people. Every turn of it runs
    confined and is never resumed, and the box deletes what it ran (spec §1), so
    without this the agent would meet each message with no memory of the last.
    It goes in the caller envelope, to the sensitive-data runner that runs the
    turn through its ZDR API key, and is deleted with the envelope when the turn ends.

    What was said, by whom, oldest first: the person's lines and the agent's
    replies, never a tool's call or result. None for an ordinary conversation."""
    session = getattr(turn, "chat_session", None)
    if session is None or not is_sensitive(session):
        return None
    from apps.canopy_sessions.models import Message  # noqa: PLC0415 — harness ↛ chat at import

    rows = (Message.objects.filter(session=session, role__in=(Message.USER, Message.ASSISTANT),
                                   sealed__isnull=False)
            .exclude(source_turn_id=turn.pk)
            .order_by("-turn_index")[:HISTORY_MESSAGES])
    out: list[dict] = []
    size = 0
    for m in rows:
        words = _open(m.sealed)
        text = str((words or {}).get("plaintext") or "").strip()
        if not text:
            continue
        size += len(text.encode())
        if size > HISTORY_BYTES:
            break
        out.append({"role": "person" if m.role == Message.USER else "agent",
                    "author": str((m.author or {}).get("name") or "") or None, "text": text})
    return out[::-1]


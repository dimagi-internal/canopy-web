"""Whether a conversation holds a host's sensitive data — THE predicate every storage
and re-exposure rule asks (spec 2026-10-22-sensitive-data-agents §4).

A session or turn is SENSITIVE when what it requires of its runner includes
`sensitive_data` (`runner_requirements`): a host that serves sensitive data requires
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
* **read back by its own people only** — the words are kept SEALED on the message,
  to a key only the conversation's people hold (`seal`, `canopy_sessions.conversation_keys`):
  canopy-web writes them and cannot read them back. The ciphertext goes only to the
  conversation's people on its chat surfaces (`reads_content`, `readable`), whose
  browsers decrypt it: never a review, a search, a fleet reader or another AI.
"""
from __future__ import annotations

import logging

from canopy_sdk import resources as resource_links

from . import runner_requirements as rr

SENSITIVE_DATA = rr.SENSITIVE_DATA
#: The refusal every re-exposure door gives, so a person reads one sentence.
REFUSAL = ("this conversation holds a host's sensitive data, so canopy does not share, export "
           "or copy it")
#: Why a sensitive conversation cannot be named: a title is shown everywhere its words are not.
NO_TITLE = ("this conversation holds a host's sensitive data, so canopy keeps no title for it")
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


def take_on(session, turn) -> None:
    """Raise `session` to what `turn` requires when the turn asks more of a runner
    than its conversation does (it was started from a sensitive turn or
    conversation). Requirements only grow. Run when a turn joins a conversation
    and again before its rows land, so the session that every reader's
    `reads_content` asks says what the sealing said."""
    if session is None or turn is None:
        return
    reqs = rr.requirements_of(turn)
    if not reqs or reqs <= rr.requirements_of_session(session):
        return
    from apps.canopy_sessions.services import add_runner_requirements  # noqa: PLC0415 — harness ↛ chat at import

    add_runner_requirements(session, reqs)


def seals(session, turn=None) -> bool:
    """THE decision whether rows landing in `session` (written by `turn`, when one
    did) are sealed: the session's own sensitivity, after it has taken on the
    turn's (`take_on`). One answer for the writer and every reader, so a
    sensitive turn's words never land unsealed in a conversation `readable`
    would hand to anyone."""
    take_on(session, turn)
    return is_sensitive(session)


def strip_result(payload: dict | None, *, session=None, text: str = "") -> dict:
    """A tool result as a sensitive conversation stores it: which call, whether it
    failed, and the host's resource links — never what came back.

    A link (`resource_links`) is an address, not data (spec
    2026-10-23-host-held-results §6): the host keeps the artifact and a View or the
    agent reads it back from the host as the person. Kept only when it is a valid
    MCP `resource_link` under a URI scheme the conversation's site declared
    (`resource_schemes`), bounded (`canopy_sdk.resources.validate_links`); anything
    else in the result is dropped. `text` is the row's plaintext, where a runner
    puts a tool's answer."""
    payload = payload if isinstance(payload, dict) else {}
    out = {k: v for k, v in payload.items() if k not in _RESULT_KEYS}
    out["withheld"] = True
    out.pop(RESOURCE_LINKS, None)  # only ever what THIS function found and checked
    candidates = _link_candidates([payload.get(k) for k in _RESULT_KEYS] + [text])
    if candidates and session is not None:
        links = resource_links.validate_links(candidates, resource_schemes(session))
        if links:
            out[RESOURCE_LINKS] = links
    return out


# ── resource links: the host's addresses, kept where its data is not ─────────────

_log = logging.getLogger(__name__)
#: The key a stripped result keeps its links under (a Message's content, a TurnEvent's
#: payload, a live frame).
RESOURCE_LINKS = "resource_links"
#: The most text parsed looking for a gateway answer's links.
_MAX_PARSE_BYTES = 4 * 1024 * 1024
#: Who says which schemes a session's site declared: registered by `apps.host_grants`
#: (which reads the site's signed `site.json`), so this core module names no feature app.
_scheme_sources: list = []


def register_resource_schemes(source) -> None:
    """`source(session) -> iterable of schemes` joins the declared schemes."""
    if source not in _scheme_sources:
        _scheme_sources.append(source)


def resource_schemes(session) -> frozenset[str]:
    """The URI schemes the session's Connected site declared for its artifacts.
    Empty — so every link is dropped — when it declared none, or it cannot be read."""
    out: set[str] = set()
    for source in _scheme_sources:
        try:
            out.update(str(s).lower() for s in (source(session) or ()))
        except Exception:  # noqa: BLE001 — a site whose schemes cannot be read keeps no links
            _log.warning("resource schemes unreadable", exc_info=True)
    return frozenset(out)


def _link_candidates(values) -> list[dict]:
    """Every `resource_link`-typed block in a result: the host's own content blocks
    (path D), and the content of a gateway answer (`site_call`'s
    `{site, tool, content, structured, is_error}`), which reaches the transcript as
    JSON text. One level of JSON only; nothing here is trusted until
    `validate_links` has checked it."""
    out: list[dict] = []

    def blocks(value, depth: int = 0) -> None:
        if depth > 2 or len(out) > 200:
            return
        if isinstance(value, str):
            if not value or len(value) > _MAX_PARSE_BYTES or value.lstrip()[:1] not in "{[":
                return
            try:
                value = _json.loads(value)
            except ValueError:
                return
            blocks(value, depth + 1)
        elif isinstance(value, list):
            for item in value[:500]:
                if isinstance(item, dict) and item.get("type") == resource_links.RESOURCE_LINK:
                    out.append(item)
                elif isinstance(item, dict) and item.get("type") == "text":
                    blocks(item.get("text"), depth + 1)
                elif isinstance(item, (list, str)):
                    blocks(item, depth + 1)
        elif isinstance(value, dict):
            if value.get("type") == resource_links.RESOURCE_LINK:
                out.append(value)
            elif isinstance(value.get("content"), (list, str)):
                blocks(value["content"], depth + 1)

    for v in values:
        blocks(v)
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


def menu_metadata(menu) -> dict | None:
    """A blocked agent's dialog (`RunnerBinding.pending_question`) as a sensitive
    conversation keeps it: that it is waiting, who produced the dialog and its
    shape — how many options, numbered, and whether each question is pick-any —
    never the question or an option's words, which can quote the host's data.
    The digest of the words tells one ask from the next without keeping them.
    None for no dialog."""
    if not isinstance(menu, dict) or not menu:
        return None
    if menu.get("redacted") == REDACTED:
        return menu

    def numbered(options) -> list[dict]:
        rows = [o for o in (options or []) if isinstance(o, dict)]
        return [{"number": o.get("number") if isinstance(o.get("number"), int) else i,
                 "label": str(o.get("number") if isinstance(o.get("number"), int) else i),
                 "description": ""} for i, o in enumerate(rows, start=1)]

    words = {"question": menu.get("question"), "title": menu.get("title"),
             "body": menu.get("body"), "options": menu.get("options"),
             "questions": menu.get("questions")}
    out: dict = {"redacted": REDACTED, "question": "", "title": "", "body": "",
                 "options": numbered(menu.get("options")),
                 "sha256": _size_and_hash(words)[1]}
    for key in ("source", "observed_at", "selected"):
        if isinstance(menu.get(key), (bool, int, float, str)) and key in menu:
            out[key] = menu[key] if not isinstance(menu[key], str) else menu[key][:MAX_META_STRING]
    if isinstance(menu.get("questions"), list):
        out["questions"] = [
            {"index": q.get("index", i), "question": "", "header": "",
             "multi_select": bool(q.get("multi_select")), "options": numbered(q.get("options"))}
            for i, q in enumerate(q for q in menu["questions"] if isinstance(q, dict))]
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
# metadata-only columns. `sealed` is sealed to the conversation's own public key
# (`canopy_sessions.conversation_keys`, spec 2026-10-23 §2): canopy-web can write it
# and holds no key that reads it back. `readable` is the one door that hands the
# ciphertext out, and only to a reader `reads_content` admits — the conversation's
# own people, whose browsers hold the key.


def seal(session, index: int, role: str, text: str, content: dict | None) -> tuple[str, dict, str | None]:
    """(plaintext, content, sealed) as a sensitive conversation stores a Message at
    `index` of `session`.

    The columns are metadata. `sealed` holds the words, sealed to the conversation's
    key, except for a tool's result, which is never kept (`strip_result`): what a tool
    showed the person as a table or a chart is read again from the host — through the
    result's resource links, which are kept, or from the call's input — as the
    viewer, never from a copy. None when the conversation has no key (a starter
    with no vault key): then the words are not kept at all."""
    from apps.canopy_sessions import conversation_keys  # noqa: PLC0415 — harness ↛ chat at import

    content = content if isinstance(content, dict) else {}
    if role == "tool_result":
        return "", strip_result(content, session=session, text=text or ""), None
    words = {"plaintext": text or "", "content": content}
    return "", redact_payload(content), conversation_keys.seal_words(session, index, words)


def call_of(content) -> dict:
    """A TOOL_USE row's call as the server may know it, for deciding which View it
    has: in a sensitive conversation the metadata columns alone — its id, its name and
    the host tool a gateway call reached (`redact_payload`'s `input_tool`), never its
    arguments, which only the conversation's people decrypt (`sealed_call_for`)."""
    content = content if isinstance(content, dict) else {}
    if content.get("redacted") != REDACTED:
        return content
    call = {k: content[k] for k in ("id", "name") if isinstance(content.get(k), str)}
    if isinstance(content.get("input_tool"), str):
        call["input"] = {"tool": content["input_tool"]}
    return call


def sealed_call_for(session, tool_call_id: str, *, chat_surface: bool, user=None, contact=None) -> str | None:
    """The sealed words of the TOOL_USE row `tool_call_id` names, for a reader
    `reads_content` admits (an MCP Apps View's input, which their browser decrypts and
    hands the View). None for an ordinary conversation, or anyone else."""
    if not is_sensitive(session) or not reads_content(
            session, chat_surface=chat_surface, user=user, contact=contact):
        return None
    from apps.canopy_sessions import conversation_keys  # noqa: PLC0415 — harness ↛ chat at import
    from apps.canopy_sessions.models import Message  # noqa: PLC0415 — as above

    sealed = (Message.objects.filter(session=session, role=Message.TOOL_USE, content__id=tool_call_id)
              .values_list("sealed", flat=True).first())
    return sealed if conversation_keys.is_envelope(sealed) else None


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


def readable(messages, session, *, chat_surface: bool, user=None, contact=None) -> list:
    """`messages` as this reader receives them. Copies, each carrying
    `readable_sealed`: the row's sealed words (ciphertext — the reader's browser
    decrypts them with the conversation key, `conversation_keys.reader_key`) for the
    conversation's own people on its chat surfaces; None for everyone else, who read
    the metadata columns. Only a `cc1:` envelope is handed out: a row still under the
    field key waits for `reseal_sensitive_conversations`."""
    from apps.canopy_sessions import conversation_keys  # noqa: PLC0415 — harness ↛ chat at import

    rows = list(messages)
    admitted = is_sensitive(session) and reads_content(
        session, chat_surface=chat_surface, user=user, contact=contact)
    out = []
    for m in rows:
        sealed = getattr(m, "sealed", None)
        if admitted and conversation_keys.is_envelope(sealed):
            m = _copy.copy(m)
            m.readable_sealed = sealed
        out.append(m)
    return out


def sealed_key_for(session, *, chat_surface: bool, user=None, contact=None) -> dict | None:
    """What this reader's browser needs to open the conversation
    (`conversation_keys.reader_key`), behind the same gate as the words. None for an
    ordinary conversation, a reader `reads_content` refuses, or a contact (who holds
    no key)."""
    if not is_sensitive(session) or contact is not None or not reads_content(
            session, chat_surface=chat_surface, user=user, contact=contact):
        return None
    from apps.canopy_sessions import conversation_keys  # noqa: PLC0415 — harness ↛ chat at import

    return conversation_keys.reader_key(session, user)

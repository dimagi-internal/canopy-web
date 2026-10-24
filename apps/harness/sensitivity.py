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
import re
from collections.abc import Mapping

from canopy_sdk import resources as resource_links
from django.db.models import Q

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
#: Why a fork of one is refused to anyone but its owner.
NOT_OWNER_FORK = ("this conversation holds a host's sensitive data: only its owner can fork it")
#: Why a send into one is refused: only its people, in canopy, may steer its agent.
NOT_IN_IT_SEND = ("this conversation holds a host's sensitive data: only the people in it can "
                  "send into it, from canopy's chat or the site's panel")
#: Why its page (what it shows, what it can do) is refused to someone not in it, or
#: through a door that is not its chat surface (MCP, a PAT, a minted session).
NOT_IN_IT_PAGE = ("this conversation holds a host's sensitive data: only the people in it can "
                  "see or act on its page, from canopy's chat or the site's panel")
#: Result-bearing keys a tool result may carry, from every runner and the gateway:
#: where its links are looked for. Never kept.
_RESULT_KEYS = ("content", "text", "output", "result", "structured", "structuredContent")
#: What a stripped tool result keeps — an allowlist, as `redact_payload`'s: a key not
#: named here (a new field a runner starts sending) is dropped until someone decides it
#: is safe. Scalars only; `resource_links` is added after its own check.
_STRIPPED_KEYS = ("tool_use_id", "id", "name", "is_error", "status", "withheld")
#: The shape each kept string must have: an id the model's API or a runner minted, a
#: tool's name, a status from a short list — never free text a tool could fill.
_CALL_ID = re.compile(r"^[A-Za-z0-9_.:-]{1,128}$")
_STRIPPED_SHAPES = {"tool_use_id": _CALL_ID, "id": _CALL_ID, "name": re.compile(r"^[A-Za-z0-9_.:-]{1,200}$")}
#: Every `status` a runner reports for a finished tool (ACP's `ToolCallStatus`, Claude
#: Code's, the gateway's); anything else is dropped.
TOOL_STATUSES = frozenset({"pending", "in_progress", "running", "completed", "complete", "failed",
                           "error", "cancelled", "canceled", "success", "ok"})
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


def conversation_of(turn):
    """The conversation a turn's words belong to: a chat turn's `chat_session`, or the
    `canopy_session` an agent, board or email turn ran in. None for neither. Every
    sensitivity rule about a turn's conversation asks this, never one link alone."""
    if turn is None:
        return None
    if turn.chat_session_id:
        return turn.chat_session
    if getattr(turn, "canopy_session_id", None):
        return turn.canopy_session
    return None


def turns_of_q(session_id):
    """The Q for every turn whose conversation is `session_id` (`conversation_of`)."""
    return Q(chat_session_id=session_id) | Q(canopy_session_id=session_id)


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


def strip_result(payload: dict | None, *, session=None, text: str = "", tool: str = "",
                 calls: Mapping | None = None) -> dict:
    """A tool result as a sensitive conversation stores it: which call, whether it
    failed, and the host's resource links — never what came back.

    A link (`resource_links`) is an address, not data (spec
    2026-10-23-host-held-results §6): the host keeps the artifact and a View or the
    agent reads it back from the host as the person. Kept only when the result is the
    SITE's own (`tool`, the producing call's name — `producing_tool` finds it from
    `calls`, the batch's `{call id: name}`, or the stored call — must be the site's
    `site_call` or one of its tools: `register_site_tools`), and the link is a valid MCP
    `resource_link` of the shape the site declared (`resource_schemes`: `<scheme>://` and
    a url-safe id, of the declared length), bounded
    (`canopy_sdk.resources.validate_links`). Anything else in the result is dropped, and
    of the rest only `_STRIPPED_KEYS` survive, each in its own shape (`_STRIPPED_SHAPES`,
    `TOOL_STATUSES`). Each kept link is `{type, uri, name, mimeType}` with the URI's
    scheme as its name, so the host's words never rest here in plaintext. `text` is the
    row's plaintext, where a runner puts a tool's answer."""
    payload = payload if isinstance(payload, dict) else {}
    out = {}
    for key in _STRIPPED_KEYS:
        value = payload.get(key)
        if isinstance(value, (bool, int, float)):
            out[key] = value
        elif isinstance(value, str) and _kept_string(key, value):
            out[key] = value
    out["withheld"] = True
    # The runner's row carries the links it pulled out of the raw result before cutting
    # its text (`canopy_transcript.tool_result_links`): read first, as they are whole.
    # Like everything else here they are only candidates until checked below.
    kept = payload.get(RESOURCE_LINKS)
    candidates = [b for b in kept if isinstance(b, dict)] if isinstance(kept, list) else []
    candidates += _link_candidates([payload.get(k) for k in _RESULT_KEYS] + [text])
    if candidates and session is not None and is_site_tool(
            session, tool or producing_tool(session, payload, calls)):
        links = resource_links.validate_links(candidates, resource_schemes(session), keep_name=False)
        if links:
            out[RESOURCE_LINKS] = links
    return out


def _kept_string(key: str, value: str) -> bool:
    if key == "status":
        return value in TOOL_STATUSES
    if key == "withheld":
        return False
    shape = _STRIPPED_SHAPES.get(key)
    return bool(shape and shape.match(value))


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


def resource_schemes(session) -> dict[str, int | None]:
    """The URI schemes the session's Connected site declared for its artifacts, each
    with the exact length of its result ids where it declared one (else None) — as
    `canopy_sdk.resources.validate_links` takes them. Empty — so every link is dropped —
    when it declared none, or it cannot be read."""
    out: dict[str, int | None] = {}
    for source in _scheme_sources:
        try:
            got = source(session) or {}
            items = got.items() if isinstance(got, Mapping) else ((s, None) for s in got)
            for scheme, length in items:
                out[str(scheme).lower()] = length
        except Exception:  # noqa: BLE001 — a site whose schemes cannot be read keeps no links
            _log.warning("resource schemes unreadable", exc_info=True)
    return out


#: Who says whether a tool is the session's site's own (`apps.host_grants`).
_site_tool_sources: list = []


def register_site_tools(source) -> None:
    """`source(session, tool_name) -> bool`: the tool is the session's site's own."""
    if source not in _site_tool_sources:
        _site_tool_sources.append(source)


def is_site_tool(session, tool: str) -> bool:
    """Whether `tool` is the session's Connected site's own tool (canopy's `site_call`,
    or the site's MCP tool reached directly): the only results whose links are kept.
    False when no source says so, or the tool is unknown."""
    if not tool:
        return False
    for source in _site_tool_sources:
        try:
            if source(session, tool):
                return True
        except Exception:  # noqa: BLE001 — a tool canopy cannot place keeps no links
            _log.warning("site tool unreadable", exc_info=True)
    return False


def calls_in(rows, *, kind_key: str = "role", use: str = "tool_use", payload_key: str = "content") -> dict:
    """`{call id: tool name}` for the tool calls in a batch of rows, so a result in the
    same batch as its call can be placed (`strip_result(calls=)`) before the call is
    stored."""
    out: dict[str, str] = {}
    for row in rows or ():
        if not isinstance(row, Mapping) or row.get(kind_key) != use:
            continue
        body = row.get(payload_key)
        if isinstance(body, Mapping) and isinstance(body.get("id"), str) and isinstance(body.get("name"), str):
            out[body["id"]] = body["name"]
    return out


def producing_tool(session, payload: Mapping, calls: Mapping | None = None) -> str:
    """The name of the tool call a result answers: from `calls` (this batch), else the
    stored call — a `tool_use` Message or a `tool_start` event in `session` — else ""."""
    call_id = payload.get("tool_use_id") or payload.get("id")
    if not isinstance(call_id, str) or not call_id:
        return ""
    if calls and isinstance(calls.get(call_id), str):
        return calls[call_id]
    if session is None or getattr(session, "pk", None) is None:
        return ""
    from apps.canopy_sessions.models import Message  # noqa: PLC0415 — harness ↛ chat at import

    from .models import TurnEvent  # noqa: PLC0415 — as above

    name = (Message.objects.filter(session_id=session.pk, role=Message.TOOL_USE, content__id=call_id)
            .values_list("content__name", flat=True).first())
    if not name:
        name = (TurnEvent.objects.filter(turn__chat_session_id=session.pk, kind="tool_start",
                                         payload__id=call_id)
                .values_list("payload__name", flat=True).first())
    return name if isinstance(name, str) else ""


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
            else:
                for key in ("links", "content"):
                    if isinstance(value.get(key), (list, str)):
                        blocks(value[key], depth + 1)

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
    return any(is_sensitive(t) for t in turns.select_related("chat_session", "canopy_session",
                                                              "parent_session")[:20])


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


def seal(session, index: int, role: str, text: str, content: dict | None, *,
         calls: Mapping | None = None) -> tuple[str, dict, str | None]:
    """(plaintext, content, sealed) as a sensitive conversation stores a Message at
    `index` of `session`.

    The columns are metadata. `sealed` holds the words, sealed to the conversation's
    key, except for a tool's result, which is never kept (`strip_result`): what a tool
    showed the person as a table or a chart is read again from the host — through the
    result's resource links, which are kept, or from the call's input — as the
    viewer, never from a copy. None when the conversation has no key (a starter
    with no vault key): then the words are not kept at all. `calls` places a result whose
    call is in the same batch and not yet stored (`calls_in`)."""
    from apps.canopy_sessions import conversation_keys  # noqa: PLC0415 — harness ↛ chat at import

    content = content if isinstance(content, dict) else {}
    if role == "tool_result":
        return "", strip_result(content, session=session, text=text or "", calls=calls), None
    words = {"plaintext": text or "", "content": content}
    return "", redact_payload(content), conversation_keys.seal_words(session, index, words, role=role)


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


def people_in(sessions, user) -> set:
    """Which of `sessions` `user` is IN (`is_conversation_person`), for a whole page in
    one query: its creator, or a participant someone added. A list asks this once,
    never per row (`tests/test_bulk_authz_parity.py`)."""
    rows = list(sessions)
    if user is None or not getattr(user, "is_authenticated", False) or not rows:
        return set()
    mine = {s.pk for s in rows if s.created_by_id == user.pk}
    rest = [s.pk for s in rows if s.pk not in mine]
    if rest:
        from apps.canopy_sessions.models import SessionParticipant  # noqa: PLC0415 — as above

        mine |= set(SessionParticipant.objects.filter(session_id__in=rest, user=user)
                    .values_list("session_id", flat=True))
    return mine


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
    """The socket's form of `request_is_chat_surface`: a contact's or a site's widget,
    or a person's own browser session. `RealtimeAuthMiddleware` says which door
    signed the socket in (`auth_method`): "session" only for a cookie a person
    signed in with — a session minted from a token is "minted"
    (`channels_auth.MINTED`), a bearer is its token's kind, and none is a person."""
    if scope.get("auth_method") == "minted":
        return False
    if scope.get("via_widget") or scope.get("contact") is not None:
        return True
    return scope.get("auth_method") == "session"


def reads_content(session, *, chat_surface: bool, user=None, contact=None) -> bool:
    """THE gate on reading a conversation back: True for any ordinary conversation;
    for a sensitive one, only its own people on its own chat surfaces."""
    if not is_sensitive(session):
        return True
    return chat_surface and is_conversation_person(session, user=user, contact=contact)


def hears_live(session, *, chat_surface: bool, user=None, contact=None) -> bool:
    """Whether this reader may be on the conversation's live socket, whose frames carry
    its words in clear: `reads_content`, and — in a sensitive conversation with a key —
    holding a copy of that key (`conversation_keys.holds_copy`). Someone given an
    ordinary conversation that later became sensitive holds none until a browser in it
    wraps them one, and until then reads only what they could open: nothing."""
    if not reads_content(session, chat_surface=chat_surface, user=user, contact=contact):
        return False
    if not is_sensitive(session) or contact is not None:
        return True
    from apps.canopy_sessions import conversation_keys  # noqa: PLC0415 — harness ↛ chat at import

    return conversation_keys.holds_copy(session, user)


#: Why the live socket turned someone in it away (`hears_live`).
NEEDS_KEY = ("this private conversation is sealed to its people, and you have not been given "
             "its key yet: ask someone in it to give you the key (People), then reload")


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


def request_is_panel_browser(request) -> bool:
    """Whether a request a site's delegated token carries comes from the person's
    browser in canopy's own panel frame (`/embed/…`, served from canopy's origin,
    so its fetches are `Sec-Fetch-Site: same-origin`) rather than from the site's
    backend calling canopy server to server. Any request with no delegated token is
    canopy's own signed-in page, and answers True.

    Not a proof: a header a host's backend could forge. It keeps what only the
    person's browser needs (`sealed_key.keyset`, the person's vault seals) out of
    every ordinary server-to-server call; the trust boundary is in
    docs/architecture/tenancy.md § Sensitive conversations."""
    if not getattr(request, "delegated_app", None):
        return True
    return (request.headers.get("Sec-Fetch-Site") or "").lower() == "same-origin"


def sealed_key_for(session, *, chat_surface: bool, user=None, contact=None,
                   keyset: bool = True) -> dict | None:
    """What this reader's browser needs to open the conversation
    (`conversation_keys.reader_key`), behind the same gate as the words. None for an
    ordinary conversation, a reader `reads_content` refuses, or a contact (who holds
    no key). `keyset=False` leaves out the reader's own vault keyset (its seals),
    which only the panel's browser needs (`request_is_panel_browser`)."""
    if not is_sensitive(session) or contact is not None or not reads_content(
            session, chat_surface=chat_surface, user=user, contact=contact):
        return None
    from apps.canopy_sessions import conversation_keys  # noqa: PLC0415 — harness ↛ chat at import

    out = conversation_keys.reader_key(session, user)
    if out is not None and not keyset:
        out = {**out, "keyset": None}
    return out

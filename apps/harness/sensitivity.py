"""Whether a conversation holds a host's sensitive data — THE predicate every storage
and re-exposure rule asks (spec 2026-10-22-phi-agents-on-zdr-runners §4).

A session or turn is SENSITIVE when what it requires of its runner includes `zdr`
(`runner_requirements`): a host that serves real data requires ZDR runners for it,
and the requirement is never dropped down a chain (§3). A requirement canopy cannot
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
* **deliver live** — a confined turn's reply reaches its asker as live frames only
  (`reply_is_live_only`), since no transcript of it ever reaches canopy.
"""
from __future__ import annotations

from . import runner_requirements as rr

ZDR = "zdr"
#: The refusal every re-exposure door gives, so a person reads one sentence.
REFUSAL = ("this conversation holds a host's sensitive data (it requires zero-data-retention "
           "runners), so canopy does not share, export or copy it")
#: Result-bearing keys a tool result may carry, from every runner and the gateway.
_RESULT_KEYS = ("content", "text", "output", "result", "structured", "structuredContent")
#: Event kinds whose payload is a tool's answer.
RESULT_KINDS = frozenset({"tool_end", "tool_result"})


def _is(reqs) -> bool:
    return ZDR in reqs or rr.UNSATISFIABLE in reqs


def is_sensitive(obj) -> bool:
    """True for a Session or a Turn whose runner requirements include `zdr`.

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


def reply_is_live_only(turn) -> bool:
    """True when a sensitive turn's reply reaches its asker only as the ledger's live
    frames. A confined turn (a caller's: a site visitor through the widget) is never
    recorded as a resumable session, and a ZDR box deletes its transcript when it
    ends, so no transcript row ever carries the reply. The frames go to whoever is
    watching and nothing stores the words (`ledger.append_events` keeps metadata)."""
    return bool(getattr(turn, "capability", "")) and is_sensitive(turn)


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
# Beyond tool results: a sensitive conversation's STORED rows keep only what
# troubleshooting needs. What the person types, what the agent says and a tool's
# arguments can all carry PHI (a name typed into a question, a case read back), so
# the stored event, the person's own Message, the prompt once the turn ends and the
# result note keep metadata only. The live frame still carries the words to the
# person watching (`ledger.append_events`), over the socket, and nothing writes them.

import hashlib as _hashlib
import json as _json

#: What a redacted row says about itself, so a reader knows content was withheld on
#: purpose rather than lost.
REDACTED = "zdr"
#: Payload keys that are metadata, never content. An allowlist: a key not named here
#: is dropped, so a new field a runner starts sending is withheld until someone
#: decides it is safe.
METADATA_KEYS = frozenset({
    "id", "tool_use_id", "name", "is_error", "status", "code", "exit_code",
    "duration_ms", "model", "kind", "session_id", "stop_reason", "withheld",
})
MAX_META_STRING = 200
WITHHELD_PREFIX = "[zdr] note withheld"


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

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
  is no transcript to read).
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

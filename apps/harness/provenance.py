"""What created a turn or a session, and why — recorded once, logged once.

THE INCIDENT. A scratch script in one Claude session posted chat messages to
agent `hal` with Jonathan's PAT. canopy-web logged nothing when it created the
session or the turns; the turn recorded `initiator_user=jjackson, via=chat,
assurance=pat` — byte-identical to the web UI — and the runner's
`chat turn=<id> created emdash task=c-…` was the only trace anywhere. Nobody
could say which token, which program, or which session the script ran inside.

THREE PIECES, all here so there is one place to read:

* `creation_fields(parent=..., **extra)` — the kwargs every Turn/Session create
  adds: `provenance` (credential, client, user agent, ip, request id, MCP tool —
  from `apps/common/request_context.py`) and the PARENT (`parent_turn`,
  `parent_session`, `parent_task`, `parent_claude_session`).
* `resolve_parent(...)` — the parent from the request's `X-Canopy-Parent-*`
  headers, a payload's `parent` object, a caller token's turn, or a server-side
  path that knows it (a transfer's source, a requeued lost turn). An id that does
  not resolve is KEPT verbatim in `provenance.parent_unresolved` and never 4xx's
  the request: provenance is a record, not a gate.
* `log_created(obj)` — ONE `TURN_CREATED` / `SESSION_CREATED` line on the
  `canopy.provenance` logger, on transaction commit (a rolled-back create never
  logs), with the same fields as `extra=` so the JSON formatter emits them as
  fields. Every creation site calls it; there is no other creation log line.

Header names are a cross-repo contract with canopy's CLI (canopy#768).
"""
from __future__ import annotations

import json
import logging
import re
import uuid

from django.db import transaction

from apps.common import request_context

logger = logging.getLogger("canopy.provenance")

#: request-context keys copied onto the record (when non-empty).
_CONTEXT_KEYS = ("request_id", "ip", "user_agent", "client", "credential", "mcp_tool")
PROMPT_HEAD = 80


def _uuid(value) -> uuid.UUID | None:
    if isinstance(value, uuid.UUID):
        return value
    try:
        return uuid.UUID(str(value).strip())
    except (ValueError, TypeError, AttributeError):
        return None


def snapshot(**extra) -> dict:
    """This request's provenance (or {} outside one), plus `extra` non-empty keys."""
    ctx = request_context.current()
    out = {k: ctx[k] for k in _CONTEXT_KEYS if ctx.get(k)}
    if ctx.get("via_mcp"):
        out["via_mcp"] = True
    for key, value in extra.items():
        if value not in (None, "", {}, []):
            out[key] = value
    # A JSONField: ids a caller passed as UUIDs (a runner pk) are stored as text.
    return json.loads(json.dumps(out, default=str))


def _merged_parent(parent) -> dict:
    """Header parent, overlaid by the explicit one (a payload `parent`, or what a
    server-side path knows). Accepts model instances under `turn` / `session`."""
    out = dict(request_context.current().get("parent") or {})
    if not parent:
        return out
    if hasattr(parent, "model_dump"):
        parent = parent.model_dump(exclude_none=True)
    for key, value in dict(parent).items():
        if value in (None, ""):
            continue
        if key == "turn":
            out["turn_id"] = value
        elif key == "session":
            out["session_id"] = value
        else:
            out[key] = value
    return out


def resolve_parent(parent=None) -> tuple[dict, dict, dict]:
    """(model fields, the parent as recorded, unresolved raw ids)."""
    from apps.canopy_sessions.models import RunnerBinding, Session

    from .models import Turn

    merged = _merged_parent(parent)
    fields: dict = {}
    unresolved: dict = {}
    recorded: dict = {}

    turn = merged.get("turn_id")
    turn_obj = turn if isinstance(turn, Turn) else None
    if turn_obj is None and turn not in (None, ""):
        pk = _uuid(turn)
        turn_obj = Turn.objects.filter(pk=pk).only("id", "chat_session_id").first() if pk else None
        if turn_obj is None:
            unresolved["turn_id"] = request_context.clean(turn)
    if turn_obj is not None:
        fields["parent_turn_id"] = turn_obj.pk
        recorded["turn_id"] = str(turn_obj.pk)

    session = merged.get("session_id")
    session_obj = session if isinstance(session, Session) else None
    if session_obj is None and session not in (None, ""):
        pk = _uuid(session)
        session_obj = Session.objects.filter(pk=pk).only("id").first() if pk else None
        if session_obj is None:
            unresolved["session_id"] = request_context.clean(session)
    session_id = session_obj.pk if session_obj is not None else None
    # Only the turn given: its own conversation is the parent session.
    if session_id is None and turn_obj is not None and turn_obj.chat_session_id:
        session_id = turn_obj.chat_session_id
    task = request_context.clean(merged.get("task") or "")
    host = request_context.clean(merged.get("host") or "")
    # Only the emdash task (a laptop session canopy never created): the binding
    # the runner reports for it names the session — when exactly one does.
    if session_id is None and task:
        bindings = RunnerBinding.objects.filter(session_key=task)
        if host:
            bindings = bindings.filter(host=host)
        if merged.get("project"):
            bindings = bindings.filter(emdash_project=str(merged["project"]))
        found = list(bindings.values_list("session_id", flat=True)[:2])
        if len(found) == 1:
            session_id = found[0]
    if session_id is not None:
        fields["parent_session_id"] = session_id
        recorded["session_id"] = str(session_id)

    if task:
        fields["parent_task"] = task[:200]
        recorded["task"] = task
    if host:
        recorded["host"] = host
    claude = request_context.clean(merged.get("claude_session_id") or "", 100)
    if claude:
        fields["parent_claude_session"] = claude
        recorded["claude_session_id"] = claude
    return fields, recorded, unresolved


def creation_fields(*, parent=None, **extra) -> dict:
    """The kwargs a Turn/Session create adds. Never raises: a provenance failure
    must not cost the turn, so the worst case is an empty record and a log line."""
    try:
        fields, recorded, unresolved = resolve_parent(parent)
        prov = snapshot(**extra)
        if recorded:
            prov["parent"] = recorded
        if unresolved:
            prov["parent_unresolved"] = unresolved
        return {"provenance": prov, **fields}
    except Exception:  # noqa: BLE001
        logger.exception("could not compute provenance")
        return {"provenance": snapshot(**extra)}


# --- the one creation log line ---------------------------------------------------

_BARE = re.compile(r"^[^\s\"'=\\]+$")


def _v(value) -> str:
    """A log value: bare when safe, else JSON-quoted (escapes quotes, newlines)."""
    if value in (None, ""):
        return "-"
    s = str(value)
    return s if _BARE.match(s) else json.dumps(s, ensure_ascii=False)


def credential_str(cred: dict | None) -> str:
    cred = cred or {}
    if not cred.get("type"):
        return ""
    return f"{cred.get('type')}:{cred.get('id') if cred.get('id') is not None else ''}:{cred.get('label') or ''}"


def _who(kind, user, contact, agent_slug) -> str:
    if user is not None:
        return f"{kind or 'user'}:{getattr(user, 'email', '') or user.pk}"
    if contact is not None:
        return f"{kind or 'contact'}:{getattr(contact, 'email', '') or contact.pk}"
    if agent_slug:
        return f"{kind or 'agent'}:{agent_slug}"
    return kind or "unknown"


def _common(prov: dict) -> dict:
    return {
        "credential": credential_str(prov.get("credential")),
        "client": prov.get("client", ""),
        "ua": prov.get("user_agent", ""),
        "ip": prov.get("ip", ""),
        "request_id": prov.get("request_id", ""),
        "mcp_tool": prov.get("mcp_tool", ""),
    }


def turn_fields(turn) -> dict:
    prov = turn.provenance or {}
    agent = turn.agent if turn.agent_id else (
        turn.chat_session.agent if turn.chat_session_id and turn.chat_session.agent_id else None)
    unresolved = prov.get("parent_unresolved") or {}
    return {
        "id": str(turn.pk),
        "agent": agent.slug if agent is not None else "",
        "project": turn.project,
        "session": str(turn.chat_session_id or ""),
        "origin": turn.origin,
        "status": turn.status,
        "via": turn.initiator_via,
        "who": _who(turn.initiator_kind, turn.initiator_user, turn.initiator_contact,
                    turn.initiator_agent),
        "assurance": turn.initiator_assurance,
        **_common(prov),
        "parent_turn": str(turn.parent_turn_id or unresolved.get("turn_id") or ""),
        "parent_session": str(turn.parent_session_id or unresolved.get("session_id") or ""),
        "parent_task": turn.parent_task,
        "parent_claude_session": turn.parent_claude_session,
        "raised_from_task": str(turn.raised_from_task_id or ""),
        "idem": turn.idempotency_key,
        "prompt_head": (turn.prompt or "")[:PROMPT_HEAD],
    }


def session_fields(session) -> dict:
    prov = session.provenance or {}
    unresolved = prov.get("parent_unresolved") or {}
    if session.created_by_id:
        who = _who("user", session.created_by, None, "")
    elif session.contact_id:
        who = _who("contact", None, session.contact, "")
    else:
        who = "system"
    return {
        "id": str(session.pk),
        "agent": session.agent.slug if session.agent_id else "",
        "project": session.project,
        "workspace": session.workspace.slug if session.workspace_id else "",
        "origin": session.origin,
        "who": who,
        **_common(prov),
        "parent_turn": str(session.parent_turn_id or unresolved.get("turn_id") or ""),
        "parent_session": str(session.parent_session_id or unresolved.get("session_id") or ""),
        "parent_task": session.parent_task,
        "parent_claude_session": session.parent_claude_session,
        "title": session.title,
    }


#: Keys that carry free text: always JSON-quoted in the line.
_QUOTED = {"prompt_head", "title", "ua"}


def format_line(event: str, fields: dict) -> str:
    parts = [event]
    for key, value in fields.items():
        if key in _QUOTED:
            parts.append(f"{key}={json.dumps(str(value or ''), ensure_ascii=False)}")
        else:
            parts.append(f"{key}={_v(value)}")
    return " ".join(parts)


def emit(obj) -> None:
    """Log one creation line now. Never raises."""
    from apps.canopy_sessions.models import Session

    try:
        if isinstance(obj, Session):
            event, fields = "SESSION_CREATED", session_fields(obj)
        else:
            event, fields = "TURN_CREATED", turn_fields(obj)
        extra = {"event": event, **{f"{event.split('_')[0].lower()}_{k}" if k == "id" else k: v
                                    for k, v in fields.items()}}
        logger.info(format_line(event, fields), extra=extra)
    except Exception:  # noqa: BLE001 — a log line is never worth a failed create
        logger.exception("could not log the creation of %r", obj)


def log_created(obj) -> None:
    """Log `obj`'s creation once its transaction commits (immediately outside one)."""
    transaction.on_commit(lambda: emit(obj))

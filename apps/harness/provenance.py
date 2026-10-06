"""What created a Turn or a Session, and why — recorded once, logged once.

The incident this answers (2026-10-05): a scratch script in one Claude session
posted chat messages to `hal` with a person's PAT. The only trace was a runner
line (`chat turn=<id> created emdash task=c-… (agent=hal)`) and
`Turn.initiator_*` = that person / chat / pat — indistinguishable from them
typing in the web UI. There was no server log of the creation at all, the PAT's
identity was discarded at auth, and nothing recorded a client, a request id or
the session the script ran inside.

Three things live here, and only here:

* **What to record** — `snapshot()` (credential, client, user agent, request id,
  ip, MCP tool) from the request in flight (`apps/common/request_context.py`),
  and `parent_fields()` which turns raw parent ids (headers, a payload `parent`
  object, a caller token, or canopy's own internal paths) into the `parent_*`
  columns. An id that does not resolve is kept verbatim in
  `provenance["parent"]` and NEVER refuses the request: provenance is a record,
  not a gate.
* **Filling it on every creation** — `pre_save` receivers (connected in
  `signals.py`) stamp any Turn or Session born without it, so a creation site
  added next year is covered without remembering to call anything. Sites that
  KNOW the parent (a transfer, a re-ask, a fork, a task dispatch) pass it to
  `turn_fields()` / `session_fields()` explicitly.
* **One log line** — `log_created()`, run on commit from a `post_save`
  receiver: `TURN_CREATED …` / `SESSION_CREATED …` on the `canopy.provenance`
  logger, with the same values as structured `extra=` fields for the JSON
  formatter.
"""
from __future__ import annotations

import json
import logging
import re
import uuid

from apps.common import request_context

logger = logging.getLogger("canopy.provenance")

#: The keys a parent may carry, whether from headers or a payload `parent` object.
PARENT_KEYS = ("turn", "session", "task", "host", "project", "claude_session")
_SNAPSHOT_KEYS = ("credential", "client", "user_agent", "request_id", "ip", "mcp_tool")
_BARE = re.compile(r'^[^\s"=]+$')
PROMPT_HEAD = 80


# -- what to record ----------------------------------------------------------

def snapshot(**extra) -> dict:
    """The request in flight, as a provenance dict. {} outside a request."""
    ctx = request_context.current()
    out = {k: ctx[k] for k in _SNAPSHOT_KEYS if ctx.get(k)}
    if ctx.get("via_mcp"):
        out["via_mcp"] = True
    out.update({k: v for k, v in extra.items() if v not in (None, "", {})})
    return out


def requested_parent(explicit=None) -> dict:
    """The parent the caller named: request headers (and a caller token's turn),
    overridden key by key by an explicit `parent` (payload or internal path).
    Values may be ids (strings) or model instances."""
    out = dict(request_context.current().get("parent") or {})
    if explicit is not None and not isinstance(explicit, dict):
        explicit = explicit.model_dump() if hasattr(explicit, "model_dump") else dict(explicit)
    for key, value in (explicit or {}).items():
        if key in PARENT_KEYS and value not in (None, ""):
            out[key] = value
    return out


def _as_uuid(value):
    if value is None or value == "":
        return None
    try:
        return uuid.UUID(str(value))
    except (TypeError, ValueError, AttributeError):
        return None


def _turn(value):
    from .models import Turn

    if isinstance(value, Turn):
        return value
    pk = _as_uuid(value)
    return Turn.objects.filter(pk=pk).first() if pk else None


def _session(value):
    from apps.canopy_sessions.models import Session

    if isinstance(value, Session):
        return value
    pk = _as_uuid(value)
    return Session.objects.filter(pk=pk).first() if pk else None


def _session_for_task(task: str, host: str = "", project: str = ""):
    """The session a runner binding maps an emdash task (or a cloud runner's
    Claude session id) to, narrowed by host/project when given."""
    from apps.canopy_sessions.models import RunnerBinding

    if not task:
        return None
    qs = RunnerBinding.objects.select_related("session").filter(session_key=task)
    if host:
        qs = qs.filter(host=host)
    if project:
        qs = qs.filter(emdash_project=project)
    binding = qs.order_by("-updated_at").first()
    return binding.session if binding is not None else None


def _raw(value) -> str:
    pk = getattr(value, "pk", None)
    return str(pk if pk is not None else value)[:200]


def parent_fields(explicit=None) -> tuple[dict, dict]:
    """(`parent_*` column values, the raw parent to record in provenance).

    Never raises on a bad id: what did not resolve is in the raw record under
    `unresolved`, and the columns stay empty."""
    raw = requested_parent(explicit)
    if not raw:
        return {}, {}
    record = {k: _raw(v) for k, v in raw.items()}
    unresolved = []
    try:
        turn = _turn(raw["turn"]) if raw.get("turn") else None
        if raw.get("turn") and turn is None:
            unresolved.append("turn")
        session = _session(raw["session"]) if raw.get("session") else None
        if raw.get("session") and session is None:
            unresolved.append("session")
        if session is None and turn is not None and turn.chat_session_id:
            session = turn.chat_session
        task = str(raw.get("task") or "")
        host = str(raw.get("host") or "")
        project = str(raw.get("project") or "")
        claude_session = str(raw.get("claude_session") or "")
        if session is None and not raw.get("session"):
            session = (_session_for_task(task, host, project)
                       or _session_for_task(claude_session))
    except Exception:  # noqa: BLE001 — a record must never fail the creation
        logger.exception("could not resolve a parent %r", record)
        return {}, {**record, "unresolved": "error"}
    if unresolved:
        record["unresolved"] = ",".join(unresolved)
    parent_task = f"{host}:{project}:{task}" if host and task else task
    fields = {
        "parent_turn": turn,
        "parent_session": session,
        "parent_task": parent_task[:300],
        "parent_claude_session": claude_session[:100],
    }
    return fields, record


def turn_fields(*, parent=None, **extra) -> dict:
    """Column values for a new Turn: `provenance` plus `parent_*`."""
    fields, record = parent_fields(parent)
    prov = snapshot(**extra)
    if record:
        prov["parent"] = record
    return {"provenance": prov, **fields}


def session_fields(*, parent=None, **extra) -> dict:
    """Column values for a new Session — the same shape as `turn_fields`."""
    return turn_fields(parent=parent, **extra)


def _has_parent(instance) -> bool:
    return bool(instance.parent_turn_id or instance.parent_session_id or instance.parent_task
                or instance.parent_claude_session or (instance.provenance or {}).get("parent"))


def stamp(instance) -> None:
    """Fill a NEW Turn/Session's provenance + parent from the request in flight,
    leaving anything its creation site already set. The `pre_save` backstop."""
    if not instance._state.adding:
        return
    if not instance.provenance:
        instance.provenance = snapshot()
    if not _has_parent(instance):
        fields, record = parent_fields()
        for key, value in fields.items():
            setattr(instance, key, value)
        if record:
            instance.provenance = {**(instance.provenance or {}), "parent": record}


#: Recorded and logged, not served: a client address is for whoever reads the
#: server's logs, not for every member who can list a workspace's turns.
_PRIVATE = ("ip",)


def public(prov) -> dict:
    """`provenance` as the API serves it."""
    return {k: v for k, v in (prov or {}).items() if k not in _PRIVATE}


def parent_of(obj) -> dict | None:
    """The parent as the API's initiator block shows it; None when there is none."""
    prov = getattr(obj, "provenance", None) or {}
    out = {
        "turn": str(obj.parent_turn_id or "") or None,
        "session": str(obj.parent_session_id or "") or None,
        "task": obj.parent_task or None,
        "claude_session": obj.parent_claude_session or None,
    }
    out = {k: v for k, v in out.items() if v}
    if prov.get("parent", {}).get("unresolved"):
        out["unresolved"] = prov["parent"]
    return out or None


# -- the one log line --------------------------------------------------------

def _fmt(value) -> str:
    if value is None or value == "":
        return "-"
    text = str(value)
    return text if _BARE.match(text) else json.dumps(text, ensure_ascii=False)


def credential_text(cred) -> str:
    """`type:id:label` — `pat:42:laptop-cli`, `session`, `-` for none."""
    if not cred:
        return ""
    parts = [str(cred.get("type") or "?")]
    if cred.get("id") is not None or cred.get("label"):
        parts += [str(cred.get("id") if cred.get("id") is not None else ""),
                  str(cred.get("label") or "")]
    return ":".join(parts)


def who_text(turn) -> str:
    """`<kind>:<email|contact|agent>` for a turn's initiator."""
    kind = turn.initiator_kind or "unknown"
    ident = ""
    if turn.initiator_user_id:
        ident = getattr(turn.initiator_user, "email", "") or str(turn.initiator_user_id)
    elif turn.initiator_contact_id:
        ident = getattr(turn.initiator_contact, "email", "") or f"contact#{turn.initiator_contact_id}"
    elif turn.initiator_agent:
        ident = turn.initiator_agent
    return f"{kind}:{ident}" if ident else kind


def _session_who(session) -> str:
    if session.created_by_id:
        return f"user:{getattr(session.created_by, 'email', '') or session.created_by_id}"
    if session.contact_id:
        return f"contact:{getattr(session.contact, 'email', '') or session.contact_id}"
    return "unknown"


def _common(obj) -> dict:
    prov = obj.provenance or {}
    return {
        "credential": credential_text(prov.get("credential")),
        "client": prov.get("client", ""),
        "ua": prov.get("user_agent", ""),
        "ip": prov.get("ip", ""),
        "request_id": prov.get("request_id", ""),
        "mcp_tool": prov.get("mcp_tool", ""),
        "parent_turn": str(obj.parent_turn_id or "") or (prov.get("parent") or {}).get("turn", ""),
        "parent_session": (str(obj.parent_session_id or "")
                           or (prov.get("parent") or {}).get("session", "")),
        "parent_task": obj.parent_task,
        "parent_claude_session": obj.parent_claude_session,
    }


def turn_record(turn) -> dict:
    agent = turn.agent.slug if turn.agent_id else (
        turn.chat_session.agent.slug if turn.chat_session_id and turn.chat_session.agent_id
        else "")
    out = {
        "id": str(turn.pk),
        "agent": agent or turn.project,
        "session": str(turn.chat_session_id or ""),
        "origin": turn.origin,
        "via": turn.initiator_via,
        "who": who_text(turn),
        "assurance": turn.initiator_assurance,
        **_common(turn),
        "raised_from_task": str(turn.raised_from_task_id or ""),
        "idem": turn.idempotency_key,
        "status": turn.status,
    }
    if (turn.provenance or {}).get("clicked_by"):
        out["clicked_by"] = turn.provenance["clicked_by"]
    out["prompt_head"] = (turn.prompt or "")[:PROMPT_HEAD]
    return out


def session_record(session) -> dict:
    return {
        "id": str(session.pk),
        "agent": session.agent.slug if session.agent_id else session.project,
        "workspace": session.workspace_id or "",
        "origin": session.origin,
        "who": _session_who(session),
        **_common(session),
        "title": (session.title or "")[:PROMPT_HEAD],
    }


def format_line(event: str, record: dict) -> str:
    parts = [event]
    for key, value in record.items():
        if key in ("prompt_head", "title"):
            parts.append(f"{key}={json.dumps(str(value or ''), ensure_ascii=False)}")
        else:
            parts.append(f"{key}={_fmt(value)}")
    return " ".join(parts)


def log_created(obj) -> None:
    """THE creation log line. Never raises — logging must not undo a creation."""
    from .models import Turn

    try:
        if isinstance(obj, Turn):
            event, record = "TURN_CREATED", turn_record(obj)
        else:
            event, record = "SESSION_CREATED", session_record(obj)
        extra = {"event": event, **{f"prov_{k}": v for k, v in record.items()}}
        logger.info(format_line(event, record), extra=extra)
    except Exception:  # noqa: BLE001
        logger.exception("could not log the creation of %r", obj)


def on_created(obj) -> None:
    """Schedule `log_created` for when the creating transaction commits."""
    from django.db import transaction

    transaction.on_commit(lambda: log_created(obj))

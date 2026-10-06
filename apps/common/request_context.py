"""What made THIS request — one record per request, readable from anywhere in it.

A scratch script in one Claude session posted chat messages to agent `hal` with
Jonathan's PAT. The only trace was the runner's "created emdash task" line and a
turn whose initiator read `jjackson / chat / pat` — exactly what the web UI
records. Nothing said WHICH token, WHICH program, or which turn of which session
the script was running inside. This module is where those facts are captured,
once, so the code that creates a turn or a session (`apps/harness/provenance.py`)
can record them without every view threading a request through.

`RequestContextMiddleware` (after `BearerTokenAuthMiddleware`, which says how the
request authenticated) stores a dict in a contextvar:

    request_id   X-Request-Id if the caller sent a sane one, else minted; echoed
    ip, user_agent
    client       X-Canopy-Client — the program, e.g. `canopy-cli/0.2.590`
    credential   {type, id, label} — `request.auth_credential`
    via_mcp, mcp_tool
    parent       {turn_id, session_id, task, host, claude_session_id} from the
                 X-Canopy-Parent-* / X-Canopy-Claude-Session headers

`RequestIdLogFilter` stamps `request_id` on every log record, so one request's
lines can be found together. Nothing here ever refuses a request: provenance is
a record, and a header that is missing or malformed is simply absent from it.
"""
from __future__ import annotations

import contextvars
import logging
import re
import uuid

#: The headers canopy's CLI sends (canopy#768, `orchestrator/provenance.py`).
#: Names are part of a cross-repo contract — change them on both sides or not at all.
HEADER_REQUEST_ID = "X-Request-Id"
HEADER_CLIENT = "X-Canopy-Client"
HEADER_PARENT_TURN = "X-Canopy-Parent-Turn"
HEADER_PARENT_SESSION = "X-Canopy-Parent-Session"
HEADER_PARENT_TASK = "X-Canopy-Parent-Task"
HEADER_PARENT_HOST = "X-Canopy-Parent-Host"
HEADER_CLAUDE_SESSION = "X-Canopy-Claude-Session"

#: parent key -> header. The keys are the `parent` payload object's too.
PARENT_HEADERS = {
    "turn_id": HEADER_PARENT_TURN,
    "session_id": HEADER_PARENT_SESSION,
    "task": HEADER_PARENT_TASK,
    "host": HEADER_PARENT_HOST,
    "claude_session_id": HEADER_CLAUDE_SESSION,
}

_REQUEST_ID = re.compile(r"^[A-Za-z0-9._:\-]{1,128}$")
_MAX = 200

_current: contextvars.ContextVar[dict | None] = contextvars.ContextVar(
    "canopy_request_context", default=None,
)


def current() -> dict:
    """This request's provenance, or {} outside a request (a management command,
    the heartbeat sweep). Callers must treat every key as optional."""
    return _current.get() or {}


def set_current(ctx: dict | None):
    """Install a context (non-HTTP doors: a WebSocket consumer, an MCP tool that
    is not a REST route). Returns the token for `reset`."""
    return _current.set(ctx)


def reset(token) -> None:
    _current.reset(token)


def clean(value, limit: int = _MAX) -> str:
    """One printable line, capped. A header is caller-controlled, and it ends up
    in a log line and a JSON column."""
    s = str(value or "").strip()
    s = "".join(ch for ch in s if ch.isprintable())
    return s[:limit]


def _header(request, name: str) -> str:
    return clean(request.META.get("HTTP_" + name.upper().replace("-", "_"), ""))


def client_ip(request) -> str:
    forwarded = (request.META.get("HTTP_X_FORWARDED_FOR") or "").split(",")[0].strip()
    return clean(forwarded or request.META.get("REMOTE_ADDR") or "", 64)


def parent_from_headers(request) -> dict:
    return {k: v for k, h in PARENT_HEADERS.items() if (v := _header(request, h))}


def build(request) -> dict:
    """The provenance of one HTTP request. Reads what the auth middleware stamped."""
    supplied = _header(request, HEADER_REQUEST_ID)
    request_id = supplied if _REQUEST_ID.match(supplied or "") else uuid.uuid4().hex
    ctx = {
        "request_id": request_id,
        "ip": client_ip(request),
        "user_agent": _header(request, "User-Agent"),
        "client": _header(request, HEADER_CLIENT),
        "credential": dict(getattr(request, "auth_credential", None) or {}),
        "via_mcp": bool(getattr(request, "via_mcp", False)),
        "mcp_tool": clean(getattr(request, "mcp_tool", "") or ""),
        "parent": parent_from_headers(request),
    }
    # An MCP tool call is dispatched in-process, so its OWN request carries an
    # httpx user agent and no client headers; what the MCP client actually sent
    # rides in the principal (`apps/mcp/api_tools._principal`).
    outer = getattr(request, "mcp_outer", None) or {}
    for key in ("ip", "user_agent", "client"):
        if outer.get(key):
            ctx[key] = clean(outer[key])
    if ctx["via_mcp"] and not ctx["client"]:
        ctx["client"] = "mcp"
    ctx["parent"] = {**(outer.get("parent") or {}), **ctx["parent"]}
    # A confined session's caller token names the turn it was minted for — that
    # turn IS the parent of anything the session creates, and it is the one parent
    # nobody had to send.
    if ctx["credential"].get("turn_id") and not ctx["parent"].get("turn_id"):
        ctx["parent"]["turn_id"] = str(ctx["credential"]["turn_id"])
    return ctx


class RequestContextMiddleware:
    """Install `build(request)` for the request's duration; echo X-Request-Id."""

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        ctx = build(request)
        request.request_id = ctx["request_id"]
        token = _current.set(ctx)
        try:
            response = self.get_response(request)
        finally:
            _current.reset(token)
        try:
            response[HEADER_REQUEST_ID] = ctx["request_id"]
        except Exception:  # noqa: BLE001 — a streaming/odd response must not fail on a header
            pass
        return response


class RequestIdLogFilter(logging.Filter):
    """Stamp `request_id` on every record ("" outside a request)."""

    def filter(self, record: logging.LogRecord) -> bool:
        if not getattr(record, "request_id", ""):
            record.request_id = current().get("request_id", "")
        return True

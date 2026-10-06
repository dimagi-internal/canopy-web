"""WHO is calling, from WHAT, on behalf of WHICH parent — for the request in flight.

Written after an incident (2026-10-05): a scratch script in one Claude session
posted chat messages to an agent with a person's PAT, and the only trace was a
runner log line plus `initiator_via=chat / assurance=pat` — byte-identical to the
web UI. Nothing said a token (which one?), a script (which?), or a parent session
(whose?) was behind it. This module captures that, once, at the edge:

* `RequestContextMiddleware` (after `BearerTokenAuthMiddleware`, so the
  credential is known) mints or accepts an `X-Request-Id`, echoes it on the
  response, and binds a contextvar for the life of the request.
* `RequestIdFilter` stamps that id on every log record emitted meanwhile.
* `apps/harness/provenance.py` reads the contextvar when a Turn or Session is
  created, so no view has to thread a request through to the service layer.

Everything here is CLAIMED by the caller except the credential: the user agent,
`X-Canopy-Client` and the parent headers are correlation material a caller can
set to anything. They are recorded verbatim (clamped) and never authorize.
"""
from __future__ import annotations

import contextvars
import logging
import re
import uuid
from collections.abc import Callable, Iterator
from contextlib import contextmanager

from django.http import HttpRequest, HttpResponse

#: The request-correlation header — accepted when well-formed, minted otherwise,
#: and always echoed on the response.
REQUEST_ID_HEADER = "X-Request-Id"
#: What program is calling ("canopy-runner", "e2e_session_chat.py", "canopy-cli").
CLIENT_HEADER = "X-Canopy-Client"
#: Parent headers: the turn / session / emdash task / host / Claude session the
#: CALLER was running inside when it made this request. The `canopy` CLI fills
#: them from CANOPY_TURN_ID / CANOPY_SESSION_ID / CANOPY_EMDASH_TASK /
#: CANOPY_HOST / CLAUDE_SESSION_ID, which the runners export into every session
#: they launch.
PARENT_HEADERS = {
    "turn": "X-Canopy-Parent-Turn",
    "session": "X-Canopy-Parent-Session",
    "task": "X-Canopy-Parent-Task",
    "host": "X-Canopy-Parent-Host",
    "claude_session": "X-Canopy-Claude-Session",
}

_REQUEST_ID_RE = re.compile(r"^[A-Za-z0-9._:\-]{1,128}$")
_CONTROL_RE = re.compile(r"[\x00-\x1f\x7f]")
MAX_HEADER = 200

_current: contextvars.ContextVar[dict | None] = contextvars.ContextVar(
    "canopy_request_context", default=None,
)


def current() -> dict:
    """The context of the request in flight, or {} outside one."""
    return _current.get() or {}


def request_id() -> str:
    return current().get("request_id", "")


@contextmanager
def bound(info: dict) -> Iterator[dict]:
    """Bind `info` as the current context for the duration of the block."""
    token = _current.set(info)
    try:
        yield info
    finally:
        _current.reset(token)


def clean(value, limit: int = MAX_HEADER) -> str:
    """A header value safe to log and store: control characters out, clamped."""
    return _CONTROL_RE.sub("", str(value or "")).strip()[:limit]


def mint_request_id(raw: str = "") -> str:
    raw = (raw or "").strip()
    return raw if _REQUEST_ID_RE.match(raw) else uuid.uuid4().hex


def client_ip(request: HttpRequest) -> str:
    """First X-Forwarded-For entry (the ALB's), else REMOTE_ADDR. Correlation,
    not evidence — see apps/tokens/audit.client_ip."""
    forwarded = (request.META.get("HTTP_X_FORWARDED_FOR") or "").split(",")[0].strip()
    return clean(forwarded or request.META.get("REMOTE_ADDR") or "", 64)


def _meta(request: HttpRequest, header: str) -> str:
    return clean(request.META.get("HTTP_" + header.upper().replace("-", "_"), ""))


def credential_of(request: HttpRequest) -> dict | None:
    """The credential that authenticated this request, as stamped by
    `BearerTokenAuthMiddleware` (`request.auth_credential`), or a browser session."""
    cred = getattr(request, "auth_credential", None)
    if cred:
        return dict(cred)
    user = getattr(request, "user", None)
    if user is not None and getattr(user, "is_authenticated", False):
        return {"type": "session", "id": None, "label": ""}
    return None


def parent_from_headers(request: HttpRequest) -> dict:
    out = {}
    for key, header in PARENT_HEADERS.items():
        value = _meta(request, header)
        if value:
            out[key] = value
    return out


def from_request(request: HttpRequest, *, request_id_value: str | None = None) -> dict:
    """The context for one HTTP request. Empty values are dropped."""
    info = {
        "request_id": request_id_value or mint_request_id(_meta(request, REQUEST_ID_HEADER)),
        "ip": client_ip(request),
        "user_agent": _meta(request, "User-Agent"),
        "client": _meta(request, CLIENT_HEADER),
        "credential": credential_of(request),
        "via_mcp": bool(getattr(request, "via_mcp", False)),
        "mcp_tool": clean(getattr(request, "mcp_tool", "") or ""),
        "parent": parent_from_headers(request),
    }
    # A confined session's caller token names the turn it was minted for: that
    # turn is the parent of anything this request starts, whatever the headers say.
    cred = info["credential"] or {}
    if cred.get("turn_id") and not info["parent"].get("turn"):
        info["parent"]["turn"] = str(cred["turn_id"])
    return {k: v for k, v in info.items() if v not in ("", None, {}, False)}


class RequestContextMiddleware:
    """Bind `from_request` for the request and echo `X-Request-Id`.

    After `BearerTokenAuthMiddleware` in `MIDDLEWARE`: the credential it stamps
    is half of what this records.
    """

    def __init__(self, get_response: Callable[[HttpRequest], HttpResponse]) -> None:
        self.get_response = get_response

    def __call__(self, request: HttpRequest) -> HttpResponse:
        info = from_request(request)
        request.request_id = info["request_id"]
        request.provenance_context = info
        with bound(info):
            response = self.get_response(request)
        try:
            response[REQUEST_ID_HEADER] = info["request_id"]
        except Exception:  # noqa: BLE001 — a header must never fail a response
            pass
        return response


class RequestIdFilter(logging.Filter):
    """Stamp `record.request_id` ("" outside a request) on every record."""

    def filter(self, record: logging.LogRecord) -> bool:
        if not getattr(record, "request_id", ""):
            record.request_id = request_id()
        return True

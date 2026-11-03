"""A screen's call to a provider: `POST /api/connections/{provider}/request`.

A dynamic view or app page never holds a token. It asks here, on the signed-in person's session,
and this does the checks in order, each before anything more is read: a session, the surface and
whether the person may open it, the allowlist (`calls`), what that surface declared
(`declarations`), whose connection, a write's operation, the rate limit. Only then is a token read
(`server_tokens`) and the provider called, with no transaction held, no redirect followed, and
the answer capped.

The route is a plain `def`: Django runs it on a worker thread, as it does every route that calls a
provider, so the wait for Salesforce blocks that thread and not the event loop.

Every refusal is a `services.ConnectionError` whose message is a sentence for the screen
(`problems.problem_for_call`). A token appears in none of them, in no log and no Event: an answer
is scrubbed of it as soon as it is read, and an exception from `requests`, which can quote the URL,
is never passed on. A write is an Event once it has been sent, whatever came back. A read is a log
line. Neither says what was asked or answered, only where.
"""
from __future__ import annotations

import json
import time
from dataclasses import dataclass, replace
from urllib.parse import quote, quote_plus

import requests
from django.urls import reverse

from apps.common.rate_limit import over
from apps.common.views_debug import is_machine

from . import calls, declarations, providers, server_key, server_tokens, services
from .models import Connection

CALLS_PER_MINUTE = 300
RATE_WINDOW_SECONDS = 60
CONNECT_SECONDS, READ_SECONDS = 5, 30
MAX_REQUEST_BYTES = 1024 * 1024
MAX_RESPONSE_BYTES = 5 * 1024 * 1024
MAX_TEXT_CHARS = 1000
_CHUNK_BYTES = 64 * 1024
_REDACTED = b"[redacted]"

_monotonic = time.monotonic


def _title(provider: str) -> str:
    return getattr(providers.CLASSES.get(provider), "title", provider)


@dataclass
class _Sent:
    """What an audit line can say about a call: whether anything left, and what came back."""
    sent: bool = False
    status: int | None = None
    outcome: str = "answered"


def call(request, provider: str, payload) -> dict:
    """The provider's `{status, body}` (`text` too, for an answer that isn't JSON), or a
    `services.ConnectionError`. Takes the request, not only its user: whether a session is behind it,
    and which workspaces this request may read a view in, are the request's."""
    _require_session(request)
    if len(request.body) > MAX_REQUEST_BYTES:  # what arrived, not what it parsed to
        raise services.ConnectionError("request_too_large", "That is too much to send in one call.")
    user = request.user
    surface = declarations.resolve_surface(request, payload.surface.model_dump())
    matched, query = _allowed(provider, payload)
    if not declarations.allows(surface.declared, provider, payload.as_, payload.agent, matched, payload.method):
        raise services.ConnectionError("not_declared", f"This screen didn't declare that {_title(provider)} call, "
                                                       "so it can't make it.")
    try:
        conn = (_shared_connection(user, provider, surface, payload.agent) if payload.as_ == "shared"
                else _viewer_connection(user, provider, surface))
        if matched.rule.kind == "write":
            _check_operation(user, conn, surface, matched, payload.operation_id)
        if over(f"connections:calls:{user.pk}:{provider}", CALLS_PER_MINUTE, RATE_WINDOW_SECONDS):
            raise services.ConnectionError("rate_limited", f"Too many {_title(provider)} calls in the last minute; "
                                                           "wait a moment and try again.")
        sent = _Sent()
        try:
            return _exchange(conn, provider, matched, query, payload.body, sent)
        finally:
            if sent.sent:
                _audit(conn, user, surface, matched, payload.operation_id, sent)
    except services.NotConnected as exc:
        exc.members = {"connect_url": request.build_absolute_uri(
            reverse("connections_start", args=[provider, surface.workspace]))}
        raise


def _require_session(request) -> None:
    """A person at a browser, and nobody else: no token (`is_machine`), and no site's token riding
    along with the cookie, which leaves the request looking signed in and takes off its CSRF check."""
    if is_machine(request) or getattr(request, "delegated_app", None) is not None:
        raise services.ConnectionError("session_required", "Only a person signed in to canopy-web can make this call; "
                                                           "a token can't.")


def _allowed(provider: str, payload) -> tuple[calls.Match, dict]:
    try:
        matched = calls.match(provider, payload.method, payload.path)
        query = calls.check_query(payload.query)
    except calls.NotSupported as exc:
        raise services.ConnectionError("provider_not_supported", str(exc)) from None
    except calls.NotAllowed as exc:
        raise services.ConnectionError("not_allowed", str(exc)) from None
    if payload.body is not None and matched.rule.method in ("GET", "DELETE"):
        raise services.ConnectionError("not_allowed", f"A {matched.rule.method} call can't carry a body.")
    return matched, query


def _viewer_connection(user, provider: str, surface: declarations.Surface) -> Connection:
    """The caller's own connection, in the surface's own workspace and no other."""
    title = _title(provider)
    try:
        return services.connection_for_person(user, provider, workspace=surface.workspace)
    except services.NotConnected:
        raise services.NotConnected(f"Your {title} isn't connected in this workspace; connect it to use this "
                                    "screen.") from None
    except services.ConnectionError as exc:
        if exc.code != "ambiguous":
            raise
    labels = sorted(c.account_label for c in services.person_connections(user, provider, workspace=surface.workspace))
    raise services.ConnectionError("ambiguous", f"You have more than one {title} account connected in this workspace "
                                                 f"({', '.join(labels)}). Disconnect all but one in Settings → "
                                                 "Connections.")


def _shared_connection(user, provider: str, surface: declarations.Surface, agent: str | None) -> Connection:
    """An agent's connection shared with this screen, as its admin approved. Not yet."""
    raise services.ConnectionError("needs_approval", f"This screen uses a shared {_title(provider)} account that an "
                                                     "admin hasn't approved for it yet.")


def _check_operation(user, conn: Connection, surface: declarations.Surface, matched: calls.Match,
                     operation_id: str | None) -> None:
    """A write needs a live operation: a real click, for this write. Not yet."""
    raise services.ConnectionError("needs_operation", f"Changing {_title(conn.provider)} from a screen needs a click "
                                                      "on that screen first.")


def _token(conn: Connection) -> tuple[str, str]:
    try:
        return server_tokens.access_token(conn)
    except server_key.Unavailable:
        raise services.ConnectionError("try_again", "canopy-web can't open this connection right now; try again in a "
                                                    "moment.") from None


def _exchange(conn: Connection, provider: str, matched: calls.Match, query: dict, body, sent: _Sent) -> dict:
    """Ask, and on a 401 once more with a new token. A 401 means the provider refused the call, so
    it did not land, whatever the method. Nothing else is ever asked twice."""
    token, instance_url = _token(conn)
    status, raw = _ask(provider, matched, query, body, token, instance_url, sent)
    if status == 401:
        server_tokens.forget(conn)
        token, instance_url = _token(conn)
        status, raw = _ask(provider, matched, query, body, token, instance_url, sent)
        if status == 401:
            raise services.ConnectionError("session_ended", f"{_title(provider)} ended this connection's session. "
                                                             "Connect it again in Settings → Connections.")
    return _answer(status, raw)


def _ask(provider: str, matched: calls.Match, query: dict, body, token: str, instance_url: str,
         sent: _Sent) -> tuple[int, bytes]:
    """One request: `(status, what came back, without the token)`."""
    try:
        url = calls.build_url(provider, instance_url, matched.path, query, method=matched.rule.method)
    except calls.NotAllowed as exc:
        raise services.ConnectionError("not_allowed", str(exc)) from None
    kwargs = {} if body is None else {"json": body}
    sent.sent, sent.status = True, None
    too_large, no_answer = False, False
    status, raw = 0, b""
    try:
        resp = requests.request(matched.rule.method, url, allow_redirects=False, stream=True,
                                timeout=(CONNECT_SECONDS, READ_SECONDS),
                                headers={"Authorization": f"Bearer {token}", "Accept": "application/json"}, **kwargs)
        try:
            status = sent.status = resp.status_code
            raw, too_large, no_answer = _read(resp)
        finally:
            resp.close()
    except requests.RequestException:
        no_answer = True
    if no_answer:
        sent.outcome = "no_answer"
        raise _no_answer(provider, matched)
    if too_large:
        sent.outcome = "too_large"
        raise services.ConnectionError("response_too_large", f"{_title(provider)}'s answer is too large for a screen; "
                                                             "ask for less of it.")
    if 300 <= status < 400:
        sent.outcome = "redirected"
        raise services.ConnectionError("provider_unavailable", f"{_title(provider)} could not be reached; try again.")
    return status, _without(raw, token)


def _read(resp) -> tuple[bytes, bool, bool]:
    """`(what came, too large, ran out of time)`. The timeout only bounds each wait for bytes, so
    the whole read has its own budget: a slow drip must not hold a worker for ever."""
    deadline = _monotonic() + READ_SECONDS
    raw = bytearray()
    for chunk in resp.iter_content(chunk_size=_CHUNK_BYTES):
        raw += chunk
        if len(raw) > MAX_RESPONSE_BYTES:
            return b"", True, False
        if _monotonic() > deadline:
            return b"", False, True
    return bytes(raw), False, False


def _no_answer(provider: str, matched: calls.Match) -> services.ConnectionError:
    title = _title(provider)
    if matched.rule.kind == "write":  # it may have landed, so this is never tried again
        return services.ConnectionError("provider_unavailable", f"{title} didn't answer. Check whether the change was "
                                                                "made before trying again.")
    return services.ConnectionError("provider_unavailable", f"{title} could not be reached; try again.")


def _without(raw: bytes, token: str) -> bytes:
    for spelled in (token, quote(token, safe=""), quote_plus(token)):
        raw = raw.replace(spelled.encode(), _REDACTED)
    return raw


def _answer(status: int, raw: bytes) -> dict:
    if not raw:
        return {"status": status, "body": None}
    try:
        return {"status": status, "body": json.loads(raw)}
    except (ValueError, RecursionError):
        return {"status": status, "body": None, "text": raw.decode("utf-8", "replace")[:MAX_TEXT_CHARS]}


def _audit(conn: Connection, user, surface: declarations.Surface, matched: calls.Match, operation_id: str | None,
           sent: _Sent) -> None:
    """A write as an Event in the connection's workspace, a person's connection too: `Subject.of` has
    no workspace for one, and who changed what from which screen is the workspace's history."""
    kind = "connection_call" if matched.rule.kind == "write" else "connection_read"
    subject = replace(services.Subject.of(conn), workspace=conn.app.workspace)
    method, path = matched.rule.method, matched.path
    services.record(subject, kind, by=user, surface=f"{surface.kind}:{surface.ref}", revision=surface.revision,
                    operation_id=operation_id, method=method, path=path, status=sent.status, outcome=sent.outcome,
                    summary=f"{method} {path} on {conn.provider} from {surface.kind} {surface.ref}: "
                            f"{sent.status or sent.outcome}")

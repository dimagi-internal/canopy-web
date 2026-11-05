"""A screen's call to a provider: `POST /api/connections/{provider}/request`.

A dynamic view or app page never holds a token. It asks here, on the signed-in person's session,
and this does the checks in order, each before anything more is read: a session, the surface and
whether the person may open it, the allowlist (`calls`), what that surface declared
(`declarations`), whose connection, a write's operation, the rate limit. Only then is a token read
(`server_tokens`) and the provider called, with no transaction held, no redirect followed, the
answer capped, and a limit on how many wait at once and how long each may take.

The route is a plain `def`: Django runs it on a worker thread, as it does every route that calls a
provider, so the wait for Salesforce blocks that thread and not the event loop. That is why
`_in_flight` exists, and why the read has a wall-clock limit.

Every refusal is a `services.ConnectionError` whose message is a sentence for the screen
(`problems.problem_for_call`). A token appears in none of them, in no log and no Event: an answer
is scrubbed of it as soon as it is read, and an exception from `requests`, which can quote the URL,
is never passed on. A write is an Event once it has been sent, whatever came back. A read is a log
line. Neither says what was asked or answered, only where.
"""
from __future__ import annotations

import json
import threading
import time
from collections import Counter
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, replace
from datetime import timedelta
from urllib.parse import quote, quote_plus
from uuid import UUID

import orjson
import requests
import urllib3
from django.core.cache import cache
from django.db.models import DateTimeField, F, Value
from django.db.models.functions import Least
from django.urls import reverse
from django.utils import timezone

from apps.api.renderers import OrjsonRenderer
from apps.common.rate_limit import over
from apps.common.views_debug import is_machine
from apps.workspaces.links import base_url

from . import calls, declarations, providers, server_key, server_tokens, services
from .models import Connection, ConnectionOperation, ConnectionSurfaceShare

CALLS_PER_MINUTE = 300
RATE_WINDOW_SECONDS = 60
CONNECT_SECONDS, READ_SECONDS = 5, 30
#: The whole call, however the bytes arrive: `READ_SECONDS` only bounds one wait for them.
TOTAL_SECONDS = 35
#: Calls waiting on a provider at once, each holding a worker thread, per person and per process.
MAX_IN_FLIGHT_PER_PERSON, MAX_IN_FLIGHT = 10, 50
LAST_USED_EVERY_SECONDS = 60
MAX_REQUEST_BYTES = 1024 * 1024
MAX_RESPONSE_BYTES = 5 * 1024 * 1024
MAX_TEXT_CHARS = 1000
#: A lease slides: it ends this long after its last write (or its opening, before any)...
OPERATION_SECONDS = 120
#: ...and this long after its opening, however it is written to.
OPERATION_HARD_LIMIT_SECONDS = 600
OPERATION_MAX_WRITES = 500
OPERATIONS_PER_MINUTE = 60
_CHUNK_BYTES = 64 * 1024
_REDACTED = b"[redacted]"

_monotonic = time.monotonic

_flying_lock = threading.Lock()
_flying: Counter = Counter()


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
    if not surface.allows(provider, payload.as_, payload.agent, matched, payload.method):
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
        with _in_flight(user.pk):
            try:
                return _exchange(conn, provider, matched, query, payload.body, sent)
            finally:
                if sent.sent:
                    _mark_used(conn)
                    _audit(conn, user, surface, matched, payload.operation_id, payload.as_, sent)
    except services.ConnectionError as exc:
        said = _said_to_this_caller(exc, provider, payload.as_, payload.agent)
        # Connecting their own account wouldn't bring an agent's back.
        if payload.as_ == "viewer" and (said.code == "not_connected" or getattr(said, "connect_again", False)):
            start = reverse("connections_start", args=[provider, surface.workspace])
            said.members = {"connect_url": f"{base_url()}{start}"}
        raise said from None


def open_operation(request, surface_ref: dict) -> dict:
    """A lease on this person writing from this screen at this revision: `{operation_id, expires_at,
    hard_limit_at, writes}`. Nothing here sees a click, which is the page's to check before it asks.
    What the lease does is limit how far a script that gets one can go."""
    _require_session(request)
    surface = declarations.resolve_surface(request, surface_ref)
    writes = _lease_writes(request.user, surface)
    if over(f"connections:operations:{request.user.pk}", OPERATIONS_PER_MINUTE, RATE_WINDOW_SECONDS):
        raise services.ConnectionError("rate_limited", "Too many clicks in the last minute; wait a moment and try "
                                                       "again.")
    now = timezone.now()
    row = ConnectionOperation.objects.create(
        user=request.user, surface_kind=surface.kind, surface_ref=surface.full_ref, revision=surface.revision,
        expires_at=now + timedelta(seconds=OPERATION_SECONDS),
        hard_limit_at=now + timedelta(seconds=OPERATION_HARD_LIMIT_SECONDS))
    return {"operation_id": row.pk, "expires_at": row.expires_at, "hard_limit_at": row.hard_limit_at, "writes": writes}


@contextmanager
def _in_flight(person) -> Iterator[None]:
    """A place among the calls this process is waiting on, or `rate_limited`. Blocking on a provider
    holds a worker thread, so one person or one slow provider can't take them all."""
    with _flying_lock:
        if _flying[person] >= MAX_IN_FLIGHT_PER_PERSON or _flying.total() >= MAX_IN_FLIGHT:
            raise services.ConnectionError("rate_limited", "Too many calls are running at once; wait a moment and "
                                                           "try again.")
        _flying[person] += 1
    try:
        yield
    finally:
        with _flying_lock:
            _flying[person] -= 1
            if _flying[person] <= 0:
                del _flying[person]


def _flying_total() -> int:
    with _flying_lock:
        return _flying.total()


def _mark_used(conn: Connection) -> None:
    """What Settings shows as last used, at most once a minute per connection."""
    if cache.add(f"connections:last_used:{conn.pk}", 1, timeout=LAST_USED_EVERY_SECONDS):
        Connection.objects.filter(pk=conn.pk).update(last_used_at=timezone.now())


def _lease_writes(user, surface: declarations.Surface) -> list[dict]:
    """What this screen can really write now: what it declares, less `as: shared` lines that
    `_shared_connection` would stop, whether by a missing approval, a new line elsewhere in the entry
    or a switch turned off. When that leaves nothing, says why rather than leasing nothing."""
    declared = surface.write_lines()
    if not declared:
        raise services.ConnectionError("not_declared", "This screen didn't declare any changes it can make.")
    stopped: dict[tuple[str, str], services.ConnectionError | None] = {}
    out: list[dict] = []
    for line in declared:
        if line["as"] == "shared":
            key = (line["provider"], line["agent"])
            if key not in stopped:
                try:
                    _shared_connection(user, key[0], surface, key[1])
                    stopped[key] = None
                except services.ConnectionError as exc:
                    stopped[key] = exc
            if stopped[key] is not None:
                continue
        shown = {"provider": line["provider"], "method": line["method"], "path": line["path"]}
        if shown not in out:
            out.append(shown)
    if not out:
        raise next(exc for exc in stopped.values() if exc is not None)
    return out


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
        raise services.NotConnected(f"Your {title} isn't connected in this workspace. Connect {title} in Settings → "
                                    "Connections.") from None
    except services.ConnectionError as exc:
        if exc.code != "ambiguous":
            raise
    labels = sorted(c.account_label for c in services.person_connections(user, provider, workspace=surface.workspace))
    raise services.ConnectionError("ambiguous", f"You have more than one {title} account connected in this workspace "
                                                 f"({', '.join(labels)}). Disconnect all but one in Settings → "
                                                 "Connections.")


def _shared_connection(user, provider: str, surface: declarations.Surface, agent: str | None) -> Connection:
    """The agent's connection shared with this screen, its project's or app's, when its admin has
    approved all this screen now declares of it. The agent and the connection's app are both of the
    surface's own workspace, or the share is no share. The surface's gate has said who may call."""
    title = _title(provider)
    key = surface.share_key
    if key is None:
        raise services.ConnectionError("needs_approval", f"This screen isn't in a project, so it can't use {agent}'s "
                                                         f"{title} account. Put it in a project, and ask an admin of "
                                                         f"{agent} to approve that project.")
    shares = list(ConnectionSurfaceShare.objects
                  .filter(kind=key[0], ref=key[1], connection__provider=provider, connection__agent__slug=agent,
                          connection__agent__workspace_id=surface.workspace,
                          connection__app__workspace_id=surface.workspace)
                  .select_related("connection__app__workspace", "connection__agent__workspace").order_by("pk"))
    if not shares:
        raise services.ConnectionError("needs_approval", f"An admin of {agent} hasn't approved this screen to use "
                                                         f"{agent}'s {title} account yet.")
    live = [s for s in shares if s.connection.state == Connection.ACTIVE and not s.connection.expired]
    if not live:
        raise services.NotConnected(f"The {title} account {agent} shared with this screen is no longer connected. An "
                                    f"admin of {agent} can connect it again.")
    usable = [s for s in live if s.connection.server_use and not s.connection.limited]
    if not usable:
        raise services.ConnectionError("server_use_off", _no_key_sentence(provider) if not server_key.available() else
                                       f"canopy-web isn't allowed to use the {title} account {agent} shared with this "
                                       f"screen. An admin of {agent} can turn that on in {agent}'s accounts.")
    if len(usable) > 1:  # no account names: which accounts an agent holds is for its admins, on its accounts tab
        raise services.ConnectionError("ambiguous", f"{agent} has shared more than one {title} account with this "
                                                    f"screen. An admin of {agent} can stop all but one.")
    share = usable[0]
    reads, writes = declarations.shared_lines(surface.current_declared, provider, agent)
    if (declarations.not_covered(reads, share.approved_reads)
            or declarations.not_covered(writes, share.approved_writes)):
        raise services.ConnectionError("needs_approval", f"This screen now asks more of {agent}'s {title} account than "
                                                         f"was approved. An admin of {agent} can approve the changes.")
    return share.connection


def _no_key_sentence(provider: str) -> str:
    """With no key there is no switch to turn on anywhere (`api._switch_shown`), for anyone."""
    return f"This workspace hasn't turned on {_title(provider)} for screens yet."


def _said_to_this_caller(exc: services.ConnectionError, provider: str, as_: str,
                         agent: str | None) -> services.ConnectionError:
    """`server_tokens` says what is wrong with a connection, not who can put it right, because that
    depends on who is looking: on `as: viewer` the caller is its owner, on `as: shared` one of an
    agent's admins can. Anything else is already said to the person at the screen."""
    title = _title(provider)
    connect = f"Connect {title} in Settings → Connections." if as_ == "viewer" else (
        f"An admin of {agent} can connect it again.")
    if isinstance(exc, server_tokens.ServerUseOff):
        if exc.why == "no_key":
            sentence = _no_key_sentence(provider)
        elif exc.why == "not_sealed":
            sentence = f"What is stored for this connection isn't sealed so that canopy-web can open it. {connect}"
        elif as_ != "viewer":
            sentence = (f"canopy-web isn't allowed to use the {title} account {agent} shared with this screen. "
                        f"An admin of {agent} can turn that on in {agent}'s accounts.")
        elif exc.why == "limited":  # no switch on a lend with limits (`api._switch_shown`)
            sentence = (f"Your {title} account was lent with limits, so screens can't use it. Disconnect it and "
                        "connect it again in Settings → Connections.")
        else:
            sentence = (f"canopy-web isn't allowed to use your {title} account. You can turn this on in Settings → "
                        "Connections.")
        return services.ConnectionError(exc.code, sentence)
    what = getattr(exc, "what", "")
    if not what:
        return exc
    said = services.ConnectionError(exc.code, f"{what} {connect}")
    said.connect_again = True
    return said


def _check_operation(user, conn: Connection, surface: declarations.Surface, matched: calls.Match,
                     operation_id: str | None) -> None:
    """A write spends one place on this person's lease for this screen at this revision."""
    _spend_write(user, operation_id, surface)


def _spend_write(user, operation_id: str | None, surface: declarations.Surface) -> None:
    """One conditional update, so two callers can't both take the last place, and it slides the lease
    in the same breath, never past its hard limit. A place is spent before the call and not given back
    if the call then fails: the count is an upper limit on what was sent."""
    pk = _operation_pk(operation_id)
    now = timezone.now()
    lease = ConnectionOperation.objects.none() if pk is None else ConnectionOperation.objects.filter(
        pk=pk, user=user, surface_kind=surface.kind, surface_ref=surface.full_ref, revision=surface.revision,
        expires_at__gt=now)
    slid = Value(now + timedelta(seconds=OPERATION_SECONDS), output_field=DateTimeField())
    if lease.filter(writes_used__lt=OPERATION_MAX_WRITES).update(writes_used=F("writes_used") + 1,
                                                                 expires_at=Least(F("hard_limit_at"), slid)):
        return
    if lease.exists():
        raise services.ConnectionError("needs_operation", f"One click can make at most {OPERATION_MAX_WRITES} "
                                                          "changes; click again to make more.")
    raise services.ConnectionError("needs_operation", "Making a change from this screen needs a click on it first.")


def _operation_pk(operation_id: str | None) -> UUID | None:
    try:
        return UUID(operation_id) if operation_id else None
    except ValueError:
        return None


def _token(conn: Connection) -> server_tokens.Access:
    try:
        return server_tokens.access(conn)
    except server_key.Unavailable:
        raise services.ConnectionError("try_again", "canopy-web can't open this connection right now; try again in a "
                                                    "moment.") from None


def _exchange(conn: Connection, provider: str, matched: calls.Match, query: dict, body, sent: _Sent) -> dict:
    """Ask, and on a 401 once more: with whatever newer token is stored, or after a forced refresh if
    this minute's is unspent (`server_tokens.forget`). A 401 means the provider refused the call, so
    it did not land, whatever the method. Nothing else is ever asked twice."""
    deadline = _monotonic() + TOTAL_SECONDS
    got = _token(conn)
    status, raw = _ask(provider, matched, query, body, got, deadline, sent)
    if status == 401:
        if server_tokens.forget(conn, got.digest):
            got = _token(conn)
            status, raw = _ask(provider, matched, query, body, got, deadline, sent)
        if status == 401:
            what = f"{_title(provider)} ended this connection's session."
            ended = services.ConnectionError("session_ended", what)
            ended.what = what
            raise ended
    return _answer(status, raw, provider, matched, sent)


def _ask(provider: str, matched: calls.Match, query: dict, body, got: server_tokens.Access, deadline: float,
         sent: _Sent) -> tuple[int, bytes]:
    """One request: `(status, what came back, without the token)`."""
    try:
        url = calls.build_url(provider, got.instance_url, matched.path, query, method=matched.rule.method)
    except calls.NotAllowed as exc:
        raise services.ConnectionError("not_allowed", str(exc)) from None
    kwargs = {} if body is None else {"json": body}
    sent.sent, sent.status = True, None
    too_large, no_answer = False, False
    status, raw = 0, b""
    # Identity: a decoder can sit on bytes across many socket reads, and the deadline is checked per read.
    headers = {"Authorization": f"Bearer {got.token}", "Accept": "application/json", "Accept-Encoding": "identity"}
    try:
        resp = requests.request(matched.rule.method, url, allow_redirects=False, stream=True,
                                timeout=(CONNECT_SECONDS, READ_SECONDS), headers=headers, **kwargs)
        try:
            status = sent.status = resp.status_code
            raw, too_large, no_answer = _read(resp, deadline)
        finally:
            resp.close()
    except (requests.RequestException, urllib3.exceptions.HTTPError, OSError):
        no_answer = True
    if no_answer:
        sent.outcome = "no_answer"
        raise _no_answer(provider, matched)
    if too_large:
        sent.outcome = "too_large"
        raise _too_large(provider, matched)
    if 300 <= status < 400:
        sent.outcome = "redirected"
        raise services.ConnectionError("provider_unavailable", f"{_title(provider)} could not be reached; try again.")
    return status, _without(raw, got.token)


def _read(resp, deadline: float) -> tuple[bytes, bool, bool]:
    """`(what came, too large, ran out of time)`. `read1` returns after one socket read, where
    `iter_content` waits to fill its chunk, so a drip can't keep it from the deadline; and no wait
    for bytes outlasts the time left, so a stall can't either."""
    body = resp.raw
    raw = bytearray()
    while True:
        left = deadline - _monotonic()
        if left <= 0:
            return b"", False, True
        _limit_wait(body, min(READ_SECONDS, left))
        chunk = body.read1(_CHUNK_BYTES, decode_content=True)
        if not chunk:
            return bytes(raw), False, False
        raw += chunk
        if len(raw) > MAX_RESPONSE_BYTES:
            return b"", True, False


def _limit_wait(body, seconds: float) -> None:
    """Set how long the next read may wait. The connection gives up its socket to the response when
    the provider says it will close, so look in both places; without either, `READ_SECONDS` stands."""
    sock = (getattr(getattr(body, "connection", None), "sock", None)
            or getattr(getattr(getattr(getattr(body, "_fp", None), "fp", None), "raw", None), "_sock", None))
    if sock is not None:
        sock.settimeout(seconds)


def _too_large(provider: str, matched: calls.Match) -> services.ConnectionError:
    if matched.rule.kind == "write":  # it landed or may have, so this must not invite a repeat
        return services.ConnectionError("response_too_large", "The change may have gone through, but the answer was "
                                                              "too large to read.")
    return services.ConnectionError("response_too_large", f"{_title(provider)}'s answer is too large for a screen; "
                                                          "ask for less of it.")


def _unreadable(provider: str, matched: calls.Match) -> services.ConnectionError:
    if matched.rule.kind == "write":
        return services.ConnectionError("response_unreadable", "The change may have gone through, but the answer "
                                                               "could not be read. Check before trying again.")
    return services.ConnectionError("response_unreadable", f"{_title(provider)} sent an answer that can't be read "
                                                           "here.")


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


def _answer(status: int, raw: bytes, provider: str, matched: calls.Match, sent: _Sent) -> dict:
    """What goes back. JSON the API's own renderer can't write (a number over 64 bits, nesting too
    deep) is refused here, where a sentence can still be said, not as a 500 after the view returns."""
    if not raw:
        return {"status": status, "body": None}
    try:
        out = {"status": status, "body": json.loads(raw)}
        OrjsonRenderer().render(None, out, response_status=200)
    except (RecursionError, orjson.JSONEncodeError):
        sent.outcome = "unreadable"
        raise _unreadable(provider, matched) from None
    except ValueError:
        return {"status": status, "body": None, "text": raw.decode("utf-8", "replace")[:MAX_TEXT_CHARS]}
    return out


def _audit(conn: Connection, user, surface: declarations.Surface, matched: calls.Match, operation_id: str | None,
           as_: str, sent: _Sent) -> None:
    """A write as an Event in the connection's workspace, a person's connection too: `Subject.of` has
    no workspace for one, and who changed what from which screen is the workspace's history. `by` is
    who was looking and `connection` whose account it was, which differ on a shared one."""
    kind = "connection_call" if matched.rule.kind == "write" else "connection_read"
    subject = replace(services.Subject.of(conn), workspace=conn.app.workspace)
    method, path = matched.rule.method, matched.path
    services.record(subject, kind, by=user, surface=f"{surface.kind}:{surface.ref}", revision=surface.revision,
                    operation_id=operation_id, method=method, path=path, status=sent.status, outcome=sent.outcome,
                    **{"as": as_},
                    summary=f"{method} {path} on {conn.provider} from {surface.kind} {surface.ref}: "
                            f"{sent.status or sent.outcome}")

"""A RELAY host: the site carries a person's conversation with a canopy agent
server-to-server, over a channel of its own (PersonalID, WhatsApp, SMS), and the
person never opens canopy.

The host is the SYSTEM OF RECORD for that person: canopy keeps the conversation
(its working sessions) and an anchor keyed by the host's own id for them, and
nothing else. Everything here is the host's server talking to canopy's
``/api/contact/…`` routes with a contact token it minted for the person — the
same routes and the same token a browser widget uses, so a relay is not a second
way into canopy but the widget's way, driven by a program:

    from canopy_sdk.host.relay import RelayClient

    relay = RelayClient(config)
    session_id = relay.open_session("personalid:jdoe", agent_slug="coach",
                                    channel="personalid", title="Coaching")
    turn = relay.send(session_id, "personalid:jdoe", "When is my next visit due?")
    relay.turn(turn["turn_id"], "personalid:jdoe")      # {"status": "done", ...}
    relay.messages(session_id, "personalid:jdoe")       # the conversation so far

* ``token_for`` mints (and caches until 5 minutes before it expires) the person's
  contact token: a visitor assertion carrying ``contract.CHANNEL_CLAIM``, bound to
  the agent (``contract.AGENT_CLAIM``). canopy records turns sent with it as
  ``relay:<site>:<channel>`` and serves the arrival as a CONTACT, never as a
  canopy account the person may also hold.
* ``send`` names the ``contract.RELAY_ORIGIN`` source, which only a relay token
  may name.
* ``send(context=...)`` attaches what the host knows about this moment, signed
  (``canopy_sdk.attested_context``); the agent reads it beside the words, labelled as
  the host's. A context about another person or conversation is ``context_mismatch``.
* A send over canopy's per-person limits is refused ``429 rate_limited``; the host
  should tell the person to wait rather than retry at once.

``sub`` is the HOST'S OWN id for the person and nothing else (``signing.py``): take
it from your own record of who is on the channel, never from the message.
"""
from __future__ import annotations

import datetime as _dt
import json
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable, Mapping

from .. import attested_context, contract
from .config import HostConfig
from .signing import MintFailed, arrival_payload, mint_contact_token

#: Re-mint a cached token this long before canopy says it expires.
REFRESH_BEFORE_SECONDS = 5 * 60
REQUEST_TIMEOUT_SECONDS = 15

#: ``(method, url, json_body_or_None, headers) -> (status, parsed_json_or_text)``.
Transport = Callable[[str, str, "dict | None", dict], tuple]


class RelayError(RuntimeError):
    """canopy refused a relay call, or could not be reached. ``code`` is canopy's
    reason where it gave one (``rate_limited``, ``context_mismatch``, …), ``status``
    its HTTP status (``None`` when canopy could not be reached)."""

    def __init__(self, message: str, *, code: str = "", status: int | None = None):
        super().__init__(message)
        self.code = code
        self.status = status


def _urllib_transport(method: str, url: str, body, headers: dict) -> tuple:
    data = json.dumps(body).encode() if body is not None else None
    request = urllib.request.Request(url, data=data, method=method,
                                     headers={"Accept": "application/json", **headers,
                                              **({"Content-Type": "application/json"} if data else {})})
    try:
        with urllib.request.urlopen(request, timeout=REQUEST_TIMEOUT_SECONDS) as response:
            raw = response.read()
            status = response.status
    except urllib.error.HTTPError as exc:
        try:
            raw = exc.read()
        except Exception:  # noqa: BLE001 - a body that cannot be read
            raw = b""
        status = exc.code
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise RelayError(f"canopy could not be reached: {type(exc).__name__}") from exc
    try:
        return status, json.loads(raw or b"null")
    except ValueError:
        return status, raw.decode(errors="replace")[:500]


def _expiry(value) -> float:
    """``expires_at`` (ISO 8601) as epoch seconds; 0 when it cannot be read, so the
    token is used once and never cached."""
    try:
        parsed = _dt.datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return 0.0
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=_dt.timezone.utc)
    return parsed.timestamp()


class RelayClient:
    """One host's relay to one canopy (``config.canopy_base_url``).

    Thread-safe; keep one per process. ``channel`` and ``agent_slug`` given here are
    the defaults for a person the client has not minted a token for yet (after a
    restart, say); every call that names them overrides them."""

    def __init__(self, config: HostConfig, *, channel: str = "", agent_slug: str = "",
                 transport: Transport | None = None, mint: Callable | None = None,
                 clock: Callable[[], float] = time.time):
        self.config = config
        self.default_channel = contract.parse_channel_claim(channel or None)
        self.default_agent_slug = contract.parse_agent_claim(agent_slug or None)
        self._transport = transport or _urllib_transport
        self._mint = mint or mint_contact_token
        self._clock = clock
        self._lock = threading.Lock()
        #: (sub, channel, agent_slug, workspace) -> (token, expires_at epoch)
        self._tokens: dict[tuple, tuple[str, float]] = {}
        #: sub -> the (channel, agent_slug, workspace) it was last minted with.
        self._last: dict[str, tuple[str, str, str]] = {}
        #: session id -> the (sub, channel, agent_slug, workspace) that opened it.
        self._sessions: dict[str, tuple] = {}

    # --- tokens -------------------------------------------------------------------

    def token_for(self, sub: str, *, name: str | None = None, channel: str, agent_slug: str,
                  workspace: str | None = None) -> str:
        """The person's contact token for ``channel`` and ``agent_slug``: cached until
        5 minutes before it expires, then minted again. ``name`` is the person's
        display name as the host knows it. ``workspace`` names the canopy tenant
        (default ``config.workspace``). Raises ``RelayError``."""
        sub = str(sub or "").strip()
        if not sub:
            raise ValueError("a relay needs the host's own id for the person")
        channel = contract.parse_channel_claim(channel or None)
        if not channel:
            raise ValueError(f"a relay names its channel ({contract.CHANNEL_PATTERN})")
        agent_slug = contract.parse_agent_claim(agent_slug or None)
        if not agent_slug:
            raise ValueError("a relay names the agent the person is talking to")
        tenant = contract.parse_workspace_claim(workspace or None) or self.config.workspace
        key = (sub, channel, agent_slug, tenant)
        now = self._clock()
        with self._lock:
            cached = self._tokens.get(key)
            self._last[sub] = (channel, agent_slug, tenant)
            if cached and cached[1] - REFRESH_BEFORE_SECONDS > now:
                return cached[0]
        payload = arrival_payload(self.config, sub, agent_slug=agent_slug, bind_agent=True,
                                  name=name or "", channel=channel, workspace=tenant or None)
        try:
            minted = self._mint(self.config, payload)
        except MintFailed as exc:
            raise RelayError(str(exc), code=_code_of(_mint_detail(str(exc))), status=exc.status) from exc
        token = str(minted["token"])
        with self._lock:
            self._tokens[key] = (token, _expiry(minted.get("expires_at")))
        return token

    def forget(self, sub: str) -> None:
        """Drop every cached token for ``sub`` (the person left the channel)."""
        with self._lock:
            for key in [k for k in self._tokens if k[0] == sub]:
                del self._tokens[key]
            self._last.pop(sub, None)
            for sid in [s for s, k in self._sessions.items() if k[0] == sub]:
                del self._sessions[sid]

    def _token(self, sub: str, session_id: str | None = None) -> str:
        with self._lock:
            opened = self._sessions.get(str(session_id)) if session_id else None
            last = self._last.get(sub)
        if opened is not None and opened[0] == sub:
            _sub, channel, agent_slug, tenant = opened
        elif last is not None:
            channel, agent_slug, tenant = last
        elif self.default_channel and self.default_agent_slug:
            channel, agent_slug, tenant = self.default_channel, self.default_agent_slug, ""
        else:
            raise RelayError("no token for this person: call token_for (or open_session) first, "
                             "or give the RelayClient a default channel and agent_slug",
                             code="no_token")
        return self.token_for(sub, channel=channel, agent_slug=agent_slug, workspace=tenant or None)

    # --- calls ----------------------------------------------------------------------

    def _call(self, method: str, path: str, sub: str, body=None, *, session_id: str | None = None):
        url = self.config.canopy_base_url + path
        for attempt in (0, 1):
            token = self._token(sub, session_id)
            status, data = self._transport(method, url, body, {"Authorization": f"Bearer {token}"})
            if status == 401 and attempt == 0:
                # Revoked or expired early: mint again once.
                with self._lock:
                    for key in [k for k, v in self._tokens.items() if v[0] == token]:
                        del self._tokens[key]
                continue
            break
        if not 200 <= int(status) < 300:
            detail = data.get("detail") if isinstance(data, Mapping) else data
            detail = str(detail or f"HTTP {status}")[:500]
            raise RelayError(f"canopy returned {status}: {detail}", code=_code_of(detail),
                             status=int(status))
        return data

    def open_session(self, sub: str, *, agent_slug: str, title: str = "", metadata: Mapping | None = None,
                     channel: str) -> str:
        """Start a conversation between the person and ``agent_slug``; returns its id.
        ``metadata`` is the host's own descriptive keys (``x_*`` and the like; canopy
        keeps only keys a host may set)."""
        token = self.token_for(sub, channel=channel, agent_slug=agent_slug)
        del token  # minted (and remembered) for this person; `_call` reads it back
        data = self._call("POST", "/api/contact/sessions", sub,
                          {"agent_slug": agent_slug, "title": title or "",
                           "metadata": dict(metadata or {})})
        session_id = str(data["id"])
        with self._lock:
            self._sessions[session_id] = (sub, contract.parse_channel_claim(channel),
                                          contract.parse_agent_claim(agent_slug), self._last[sub][2])
        return session_id

    def send(self, session_id: str, sub: str, text: str, *, context: Mapping | None = None,
             client_id: str | None = None, received_at=None) -> dict:
        """Say ``text`` in the conversation, as the person; returns canopy's answer
        (``{"turn_id", "message_id"}``). ``client_id`` makes a retry of the same
        message collapse onto the first one: pass your own message id.

        ``context`` — ``{"format": "markdown" | "json", "body": str}`` — is what the
        host knows about this moment, signed by the host (``sign_context``) and handed
        to the agent beside the words, labelled as the host's, for this turn only.

        ``received_at`` — a ``datetime`` (aware) or epoch seconds — is when YOUR site
        stored the person's message. canopy starts the turn's reply-latency trace there,
        so the wait it measures is the person's, including your own handling."""
        body = {"text": text, "origin": contract.RELAY_ORIGIN}
        if client_id:
            body["client_id"] = str(client_id)
        if received_at is not None:
            body["received_at"] = (received_at.isoformat() if isinstance(received_at, _dt.datetime)
                                   else float(received_at))
        if context is not None:
            body["context"] = self.sign_context(session_id, sub, context)
        return self._call("POST", f"/api/contact/sessions/{_q(session_id)}/send", sub, body,
                          session_id=session_id)

    def sign_context(self, session_id: str, sub: str, context: Mapping) -> str:
        """``context`` (``{"format", "body"}``) as the host's signed statement for one
        send into ``session_id`` about ``sub`` (``canopy_sdk.attested_context``)."""
        if not isinstance(context, Mapping):
            raise ValueError("context is {'format': 'markdown' | 'json', 'body': str}")
        body = context.get("body")
        if context.get("format", "markdown") == "json" and not isinstance(body, str):
            body = json.dumps(body)
        try:
            return attested_context.sign_context(
                self.config.app_name, self.config.signing_key, sub=sub, session=str(session_id),
                format=str(context.get("format") or "markdown"), body=body, now=self._clock())
        except attested_context.ContextError as exc:
            raise RelayError(exc.message, code=exc.code) from exc

    def turn(self, turn_id: str, sub: str) -> dict:
        """One turn of the person's conversation: its ``status`` and the rest of
        canopy's turn record."""
        return self._call("GET", f"/api/contact/turns/{_q(turn_id)}", sub)

    def messages(self, session_id: str, sub: str, after: int | None = None) -> list[dict]:
        """The conversation's messages, oldest first; with ``after`` (a message's
        ``turn_index``), only the ones after it."""
        out: list[dict] = []
        before = 2 ** 31 - 1
        while True:
            page = self._call("GET", f"/api/contact/sessions/{_q(session_id)}/messages?"
                              + urllib.parse.urlencode({"before": before, "limit": 200}), sub,
                              session_id=session_id)
            rows = list((page or {}).get("messages") or [])
            fresh = [m for m in rows if after is None or int(m.get("turn_index", -1)) > after]
            out = fresh + out
            if not page.get("has_more_before") or not rows or len(fresh) < len(rows):
                break
            before = min(int(m.get("turn_index", 0)) for m in rows)
        return out


def _q(value) -> str:
    return urllib.parse.quote(str(value), safe="")


def _mint_detail(message: str) -> str:
    """The ``detail`` of the problem document ``MintFailed`` quotes, else the message."""
    _head, sep, body = str(message or "").partition(": ")
    if sep:
        try:
            doc = json.loads(body)
        except ValueError:
            return body
        if isinstance(doc, Mapping) and doc.get("detail"):
            return str(doc["detail"])
    return str(message or "")


def _code_of(detail: str) -> str:
    """canopy names a refusal ``<code>: <words>``; the code, else ""."""
    head, sep, _ = str(detail or "").partition(":")
    head = head.strip()
    return head if sep and head.isidentifier() else ""

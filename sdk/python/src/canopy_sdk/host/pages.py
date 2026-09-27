"""Which page the visitor is on, and what that page lets canopy do as them.

One registry — page → scopes — decided HERE, server-side, and two ways for a
mint request to say which registered page it came from. Both are
``registry.scopes_for(value, user_id)``; they differ only in what ``value`` is.

**Signed mode (``PageTokens``, ``mode == "signed"``) — a server-rendered page.**
The server renders the route, so it KNOWS the page: the rendered HTML carries a
token signed with the host's own secret naming the route and the user, the
widget hands it back when it mints, and the host reads the page's scopes from
the registry NOW (not from the token), so removing a page takes effect on the
next mint. canopy never sees this token.

**Key mode (``PageRegistry``, ``mode == "key"``) — a single-page app.** The
server never renders a route, so it cannot observe which one is on screen, and
a signed token would only sign whatever the browser asked for. So the browser
NAMES the page — a key (``"opp-workbench"``), or its path when the host
registers ``patterns`` to recognise one — and everything that matters is still
decided server-side:

* a value the registry does not know gets NO grant;
* a known one gets the scopes the registry says, never scopes the browser sent
  — a page key can only SELECT among scopes the host registered;
* those scopes are read-only by default: a scope whose name does not end in
  ``:read`` is refused at construction unless listed in ``writable_scopes``,
  because in this mode the browser, not the server, picks the page;
* the delegated token still runs AS the visitor, so it reaches no more than the
  visitor's own ACL already does.

A browser that names the wrong page therefore picks among read-only views of
its own data, nothing more. That is the trust argument, and it is why key mode
is safe without a signature — and why it refuses write scopes by default.

``PageTokens`` was extracted from connect-labs ``connect_labs/labs/canopy.py``
(HMAC-SHA256 over a timestamped payload, compared in constant time); key mode
from canopy-web ``apps/tokens/self_host.py`` and ace-web
``apps/canopy/grant.py``, which each hand-rolled it.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import re
import time
from collections.abc import Mapping, Sequence

MAX_TOKEN_LENGTH = 1024
DEFAULT_MAX_AGE_SECONDS = 2 * 60 * 60
DEFAULT_SALT = "canopy_sdk.host.page"
#: The longest page key or path key mode looks at.
MAX_PAGE_KEY_LENGTH = 512
#: A scope named ``<x>:read`` is read-only by convention; key mode accepts only
#: those unless the host lists a scope in ``writable_scopes`` on purpose.
READ_ONLY_SUFFIX = ":read"

SIGNED = "signed"
KEY = "key"
MODES = (SIGNED, KEY)


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode().rstrip("=")


def _unb64(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


class PageRegistry:
    """The page → scopes registry in KEY mode: the browser names its page.

    ``pages`` maps a page key to its scopes. ``patterns`` (optional) maps a key
    to a regex (or pattern string) matched against a PATH, for a host whose
    browser sends ``location.pathname`` rather than a key; the whole path must
    match. ``scope_tools``, when given, is checked at construction: a page
    naming a scope the server does not offer is a configuration error, raised
    now rather than discovered as a grant that silently carries nothing.
    """

    mode = KEY

    def __init__(self, pages: Mapping[str, Sequence[str]], *, scope_tools: Mapping | None = None,
                 patterns: Mapping[str, object] | None = None,
                 writable_scopes: Sequence[str] = ()):
        self.pages = {str(key): tuple(scopes) for key, scopes in (pages or {}).items()}
        self.patterns = {str(key): (p if isinstance(p, re.Pattern) else re.compile(str(p)))
                         for key, p in (patterns or {}).items()}
        self.writable_scopes = frozenset(writable_scopes or ())
        if scope_tools is not None:
            for key, scopes in self.pages.items():
                if not scopes:
                    raise ValueError(f"page {key!r} is registered with no scopes")
                unknown = set(scopes) - set(scope_tools)
                if unknown:
                    raise ValueError(f"page {key!r} names scopes the server does not offer: "
                                     f"{sorted(unknown)}")
        unregistered = set(self.patterns) - set(self.pages)
        if unregistered:
            raise ValueError(f"patterns name pages that are not registered: {sorted(unregistered)}")
        if self.mode == KEY:
            for key, scopes in self.pages.items():
                writes = [s for s in scopes
                          if not s.endswith(READ_ONLY_SUFFIX) and s not in self.writable_scopes]
                if writes:
                    raise ValueError(
                        f"page {key!r} grants {sorted(writes)}, which are not read-only, and in key "
                        "mode the browser picks the page: list them in writable_scopes on purpose")

    def key_for(self, value) -> str:
        """The registered page ``value`` names — an exact key, or a path one of
        ``patterns`` matches — or ``""``. Query and fragment are ignored."""
        if not value or not isinstance(value, str) or len(value) > MAX_PAGE_KEY_LENGTH:
            return ""
        value = value.strip()
        if value in self.pages:
            return value
        path = value.split("?", 1)[0].split("#", 1)[0]
        for key, pattern in self.patterns.items():
            if key in self.pages and pattern.fullmatch(path):
                return key
        return ""

    def scopes_for_key(self, value) -> tuple[str, ...]:
        """The scopes the page ``value`` names grants, or ``()`` — never an error."""
        return self.pages.get(self.key_for(value), ())

    def scopes_for(self, value, user_id=None, **kwargs) -> tuple[str, ...]:
        """THE entry point, in either mode: the scopes the page named by
        ``value`` grants the visitor ``user_id``, or ``()``. In key mode
        ``value`` is a page key (or path) and ``user_id`` is not needed — the
        token runs as the visitor, whose own ACL applies at every tool."""
        return self.scopes_for_key(value)


class PageTokens(PageRegistry):
    """The page → scopes registry in SIGNED mode: the server renders the page
    and signs which one it is.

    Issue and read page tokens against a route → scopes registry.
    ``scope_tools`` is checked as for ``PageRegistry``. Write scopes are allowed
    here without ``writable_scopes``: the server, not the browser, decided the
    page.
    """

    mode = SIGNED

    def __init__(self, secret, pages: Mapping[str, Sequence[str]], *,
                 scope_tools: Mapping | None = None, max_age: int = DEFAULT_MAX_AGE_SECONDS,
                 salt: str = DEFAULT_SALT, writable_scopes: Sequence[str] = ()):
        if not secret:
            raise ValueError("page tokens need a server secret")
        super().__init__(pages, scope_tools=scope_tools, writable_scopes=writable_scopes)
        raw = secret.encode() if isinstance(secret, str) else bytes(secret)
        self._key = hashlib.sha256(salt.encode() + b"\x00" + raw).digest()
        self.max_age = int(max_age)

    def scopes_for(self, value, user_id=None, **kwargs) -> tuple[str, ...]:
        return self.scopes(value, user_id, **kwargs)

    def _sign(self, payload: str) -> str:
        return _b64(hmac.new(self._key, payload.encode(), hashlib.sha256).digest())

    def issue(self, route: str, user_id, *, now: float | None = None) -> str:
        """A token for this page and this user — or ``""`` for a page that is not
        registered, or no user."""
        if not route or route not in self.pages or user_id in (None, ""):
            return ""
        body = json.dumps({"p": route, "u": str(user_id),
                           "t": int(time.time() if now is None else now)},
                          separators=(",", ":"), sort_keys=True)
        payload = _b64(body.encode())
        return f"{payload}.{self._sign(payload)}"

    def scopes(self, raw, user_id, *, now: float | None = None) -> tuple[str, ...]:
        """The scopes the page named by ``raw`` grants ``user_id``, or ``()`` —
        never an error. Absent, forged, expired, another user's, or a page no
        longer registered all mean no grant; the panel carries on without one."""
        if not raw or not isinstance(raw, str) or len(raw) > MAX_TOKEN_LENGTH \
                or user_id in (None, ""):
            return ()
        payload, sep, signature = raw.partition(".")
        if not sep or not hmac.compare_digest(signature.encode(), self._sign(payload).encode()):
            return ()
        try:
            claims = json.loads(_unb64(payload))
        except (ValueError, UnicodeDecodeError):
            return ()
        if not isinstance(claims, dict) or not hmac.compare_digest(
                str(claims.get("u", "")).encode(), str(user_id).encode()):
            return ()
        issued = claims.get("t")
        current = time.time() if now is None else now
        if not isinstance(issued, int) or current - issued > self.max_age or issued - current > 60:
            return ()
        return self.pages.get(claims.get("p"), ())

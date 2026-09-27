"""The page registry and the server-signed page token.

Which pages may let canopy act as the visitor, and with which scopes, keyed by
route name — decided HERE, server-side, and never from anything the browser
sends. The rendered page carries a token signed with the host's own secret
naming its route and its user; the widget hands it back when it mints, and the
host reads the page's scopes from the registry NOW (not from the token), so
removing a page takes effect on the next mint. canopy never sees this token.

Extracted from connect-labs ``connect_labs/labs/canopy.py`` (``PAGE_SCOPES``,
``page_token``, ``scopes_for_page_token``), which used Django's signer; this is
the same construction in plain Python (HMAC-SHA256 over a timestamped payload,
compared in constant time), so a non-Django host gets it too.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import time
from collections.abc import Mapping, Sequence

MAX_TOKEN_LENGTH = 1024
DEFAULT_MAX_AGE_SECONDS = 2 * 60 * 60
DEFAULT_SALT = "canopy_sdk.host.page"


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode().rstrip("=")


def _unb64(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


class PageTokens:
    """Issue and read page tokens against a route → scopes registry.

    ``scope_tools``, when given, is checked at construction: a page naming a
    scope the server does not offer is a configuration error, raised now rather
    than discovered as a grant that silently carries nothing.
    """

    def __init__(self, secret, pages: Mapping[str, Sequence[str]], *,
                 scope_tools: Mapping | None = None, max_age: int = DEFAULT_MAX_AGE_SECONDS,
                 salt: str = DEFAULT_SALT):
        if not secret:
            raise ValueError("page tokens need a server secret")
        raw = secret.encode() if isinstance(secret, str) else bytes(secret)
        self._key = hashlib.sha256(salt.encode() + b"\x00" + raw).digest()
        self.pages = {str(route): tuple(scopes) for route, scopes in (pages or {}).items()}
        self.max_age = int(max_age)
        if scope_tools is not None:
            for route, scopes in self.pages.items():
                if not scopes:
                    raise ValueError(f"page {route!r} is registered with no scopes")
                unknown = set(scopes) - set(scope_tools)
                if unknown:
                    raise ValueError(f"page {route!r} names scopes the server does not offer: "
                                     f"{sorted(unknown)}")

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

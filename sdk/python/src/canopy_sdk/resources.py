"""Host-held results: the resource links a host's tool returns, and the host telling
canopy that one of them changed (spec ``2026-10-23-host-held-results-design``).

A host's tool returns FACTS for the model and one or more MCP ``resource_link``
content items (MCP 2025-06-18) naming results the HOST keeps — a result set, a
chart spec, a draft edit. canopy carries the links, never the data: in a sensitive
conversation it stores the links alone, and a View or the agent reads each one back
from the host, as the person, with ``resources/read``.

Both halves live here so canopy and every host read ONE definition:

* the link: ``resource_link`` builds one; ``validate_link`` is canopy's check of
  one a host returned (its shape, its bounds, and a URI scheme the host declared in
  its ``site.json`` — ``canopy_sdk.site``'s ``site.resource_schemes``);
* the scheme: ``check_scheme`` — a host's own scheme (``labs-result``), never one a
  browser or another protocol already means something by (``https``, ``data``…);
* "it changed": a host signs ``updated_notification`` with its own key (the one at
  its ``jwks_url``) and POSTs it to canopy's ``UPDATED_PATH``
  (``notify_updated``); canopy checks it with ``verify_updated_notification`` and
  tells every View subscribed to those URIs to re-read. The notification carries
  URIs only, never data: the re-read goes through ``resources/read`` as the viewer.
"""
from __future__ import annotations

import re
import time
from collections.abc import Callable, Iterable, Mapping, Sequence

from . import contract
from .contract import ContractError, constant_time_equal, normalize_url
from .jose import new_jti, sign, unverified_header
from .keys import load_private_key, public_jwk

#: The MCP content type of a link (MCP 2025-06-18, ``ResourceLink``).
RESOURCE_LINK = "resource_link"
#: A scheme's grammar (RFC 3986 §3.1), lower case, and short.
_SCHEME = re.compile(r"^[a-z][a-z0-9+.-]{0,31}$")
#: Schemes that already mean something to a browser, a client or another protocol.
#: A host's results live under a scheme of its own, so a stored link can never be
#: a web address, an inline payload or a script.
RESERVED_SCHEMES = frozenset({
    "about", "blob", "chrome", "data", "file", "filesystem", "ftp", "http", "https",
    "javascript", "mailto", "sms", "tel", "ui", "vbscript", "ws", "wss",
})
#: Bounds on one link, as canopy keeps it.
MAX_URI_LENGTH = 2048
MAX_NAME_LENGTH = 200
MAX_MIME_TYPE_LENGTH = 100
MAX_DESCRIPTION_LENGTH = 500
#: The most links canopy keeps from one tool result.
MAX_LINKS = 20

#: Where canopy takes "these resources changed", under its base URL.
UPDATED_PATH = "/api/sites/resources-updated"
#: ``typ`` of the host's signed notification: distinct from every other token
#: either side signs, so none can be replayed as another.
UPDATED_TYP = "canopy-resources-updated+jwt"
#: The notification is used once, at the moment of the POST.
UPDATED_MAX_LIFETIME = 60
#: The most URIs one notification names.
MAX_UPDATED_URIS = 50
UPDATED_REQUIRED_CLAIMS: tuple[str, ...] = ("iss", "sub", "aud", "iat", "exp", "jti", "uris")


class ResourceError(Exception):
    """A link, a scheme or a notification that is not one. ``code`` names why."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


# --- schemes ------------------------------------------------------------------------


def check_scheme(scheme: str) -> str:
    """``scheme`` lower-cased if a host may keep results under it, else ``ResourceError``."""
    value = str(scheme or "").strip().lower().rstrip(":/")
    if not _SCHEME.match(value):
        raise ResourceError("bad_scheme", f"{scheme!r} is not a URI scheme (a letter, then letters, "
                                          "digits, '+', '-' or '.'; at most 32)")
    if value in RESERVED_SCHEMES:
        raise ResourceError("reserved_scheme", f"{value!r} already means something to a browser or "
                                               "client; give your results a scheme of your own")
    return value


def scheme_of(uri: str) -> str:
    """The scheme of ``uri`` (lower case), or ``""``."""
    head, sep, _ = str(uri or "").partition(":")
    return head.lower() if sep and _SCHEME.match(head.lower()) else ""


# --- the link ---------------------------------------------------------------------


def resource_link(uri: str, name: str, *, mime_type: str = "application/json",
                  description: str = "") -> dict:
    """An MCP ``resource_link`` content item: ``{"type": "resource_link", "uri",
    "name", "mimeType", "description"?}``. Raises ``ResourceError`` on a link canopy
    would not keep."""
    link = {"type": RESOURCE_LINK, "uri": uri, "name": name, "mimeType": mime_type}
    if description:
        link["description"] = description
    checked = validate_link(link, schemes=None)
    if checked is None:
        raise ResourceError("bad_link", f"{uri!r} is not a link canopy keeps (scheme, length, name)")
    return checked


def _bounded(value, limit: int) -> str | None:
    if not isinstance(value, str):
        return None
    value = value.strip()
    return value if value and len(value) <= limit and not any(ord(c) < 32 for c in value) else None


def validate_link(block, schemes: Iterable[str] | None) -> dict | None:
    """``block`` as canopy keeps it, or None when it is not a link canopy keeps.

    It must be a ``resource_link`` with a ``uri`` (no whitespace or control
    characters, at most ``MAX_URI_LENGTH``) under a scheme in ``schemes`` (the
    host's declared list; None skips that check — the host's own builder) and never
    a reserved one, a ``name`` and a ``mimeType`` within their bounds. A
    ``description`` over its bound is dropped, not the link. Every other member is
    dropped: what canopy keeps is an address, never data (a ``_meta`` or
    ``annotations`` could carry anything). ``mime_type`` (the MCP SDK's field name
    without aliases) is read as ``mimeType``."""
    if not isinstance(block, Mapping) or block.get("type") != RESOURCE_LINK:
        return None
    uri = block.get("uri")
    if not isinstance(uri, str) or not uri or len(uri) > MAX_URI_LENGTH:
        return None
    if any(c.isspace() or ord(c) < 32 or ord(c) == 127 for c in uri):
        return None
    scheme = scheme_of(uri)
    if not scheme or scheme in RESERVED_SCHEMES:
        return None
    if schemes is not None and scheme not in {str(s).lower() for s in schemes}:
        return None
    name = _bounded(block.get("name"), MAX_NAME_LENGTH)
    mime = _bounded(block.get("mimeType", block.get("mime_type")), MAX_MIME_TYPE_LENGTH)
    if name is None or mime is None or "/" not in mime:
        return None
    out = {"type": RESOURCE_LINK, "uri": uri, "name": name, "mimeType": mime}
    description = _bounded(block.get("description"), MAX_DESCRIPTION_LENGTH)
    if description:
        out["description"] = description
    return out


def validate_links(blocks: Iterable, schemes: Iterable[str] | None) -> list[dict]:
    """Every link in ``blocks`` canopy keeps, de-duplicated by URI, at most ``MAX_LINKS``."""
    schemes = None if schemes is None else frozenset(str(s).lower() for s in schemes)
    out: list[dict] = []
    seen: set[str] = set()
    for block in blocks or ():
        link = validate_link(block, schemes)
        if link is None or link["uri"] in seen:
            continue
        seen.add(link["uri"])
        out.append(link)
        if len(out) >= MAX_LINKS:
            break
    return out


# --- "it changed": the host tells canopy ---------------------------------------------


def updated_url(canopy_url: str) -> str:
    """Where the canopy at ``canopy_url`` takes notifications — their ``aud``."""
    return normalize_url(canopy_url) + UPDATED_PATH


def _uris(value) -> list[str]:
    if not isinstance(value, (list, tuple)) or not value:
        raise ResourceError("malformed", "uris must be a non-empty list of URIs")
    out = []
    for uri in value:
        if not isinstance(uri, str) or not uri or len(uri) > MAX_URI_LENGTH or not scheme_of(uri):
            raise ResourceError("malformed", f"{uri!r} is not a URI")
        if uri not in out:
            out.append(uri)
    if len(out) > MAX_UPDATED_URIS:
        raise ResourceError("malformed", f"at most {MAX_UPDATED_URIS} URIs per notification")
    return out


def updated_notification(site_name: str, site_key, audience: str, uris: Sequence[str], *,
                         workspace: str = "", agent: str = "", now: float | None = None) -> str:
    """The host's signed "these resources changed".

    ``iss`` = ``sub`` = the site's Connected-site name, ``aud`` = ``updated_url``,
    60 seconds, single-use ``jti``, ``uris``. ``workspace`` / ``agent`` name the
    tenant exactly as ``canopy_sdk.site_agents.request_assertion`` does."""
    name = str(site_name or "").strip()
    if not name:
        raise ResourceError("not_configured", "the notification needs the site's Connected-site name")
    key = load_private_key(site_key, what="the site signing key")
    issued = int(time.time() if now is None else now)
    claims = {"iss": name, "sub": name, "aud": audience, "iat": issued,
              "exp": issued + UPDATED_MAX_LIFETIME, "jti": new_jti(), "uris": _uris(list(uris))}
    agent = contract.parse_agent_claim(agent or None)
    if agent:
        claims[contract.AGENT_CLAIM] = agent
    workspace = contract.parse_workspace_claim(workspace or None)
    if workspace:
        claims[contract.WORKSPACE_CLAIM] = workspace
    return sign(claims, key, headers={"kid": public_jwk(key)["kid"], "typ": UPDATED_TYP})


def verify_updated_notification(token: str, keys: Sequence, *, audiences: Iterable[str],
                                spend_jti: Callable[[dict], None] | None = None,
                                algorithms: Sequence[str] = contract.ASSERTION_ALGORITHMS,
                                leeway: int = contract.LEEWAY_SECONDS) -> dict:
    """canopy's check of a host's notification against that host's ``keys``: OUR
    algorithm list, ``typ``, ``aud`` one of ``audiences``, the life capped at 60s,
    ``sub`` = ``iss``, a single-use ``jti`` (``spend_jti``) and well-formed ``uris``.
    Returns the claims (``uris`` normalized) or raises ``ResourceError``."""
    import jwt

    try:
        header = unverified_header(token, algorithms=algorithms)
    except ContractError as exc:
        raise ResourceError(exc.code, exc.message) from exc
    if header.get("typ") != UPDATED_TYP:
        raise ResourceError("bad_typ", f"a resources-updated notification must have typ {UPDATED_TYP}")
    wanted = [a for a in (normalize_url(x) for x in audiences) if a]
    claims = None
    last: Exception | None = None
    for key in keys:
        try:
            claims = jwt.decode(token, key, algorithms=list(algorithms), audience=wanted, leeway=leeway,
                                options={"require": list(UPDATED_REQUIRED_CLAIMS),
                                         "verify_aud": True, "verify_exp": True})
            break
        except jwt.InvalidAudienceError as exc:
            raise ResourceError("wrong_audience", "the notification is addressed to a different canopy") from exc
        except jwt.ExpiredSignatureError as exc:
            raise ResourceError("expired", "the notification has expired") from exc
        except jwt.MissingRequiredClaimError as exc:
            raise ResourceError("incomplete", f"the notification is missing {exc.claim!r}") from exc
        except Exception as exc:  # noqa: BLE001 - try the next key
            last = exc
    if claims is None:
        raise ResourceError("bad_signature", f"no key registered for this site verifies the notification ({last})")
    try:
        contract.check_lifetime(claims, UPDATED_MAX_LIFETIME, leeway=leeway, what="a resources-updated notification")
        contract.check_jti(claims)
    except ContractError as exc:
        raise ResourceError(exc.code, exc.message) from exc
    if not constant_time_equal(str(claims.get("sub") or ""), str(claims.get("iss") or "")):
        raise ResourceError("bad_subject", "a site speaks only for its own resources (sub must equal iss)")
    claims["uris"] = _uris(claims.get("uris"))
    if spend_jti is not None:
        spend_jti(claims)
    return claims


def notify_updated(canopy_url: str, site_name: str, site_key, uris: Sequence[str], *,
                   workspace: str = "", agent: str = "", post: Callable[..., tuple] | None = None) -> int:
    """Tell canopy ``uris`` changed; returns how many Views canopy told to re-read.

    Best effort by design: a View that misses a notification shows its last read
    until it reads again. Raises ``ResourceError`` (``unreachable``, or canopy's own
    refusal code) so a caller can log it; most callers should catch it."""
    from . import fetch

    url = updated_url(canopy_url)
    token = updated_notification(site_name, site_key, url, uris, workspace=workspace, agent=agent)
    sender = post or fetch.post_json
    try:
        status, raw, _headers = sender(url, {}, headers={"Authorization": f"Bearer {token}"})
    except fetch.FetchError as exc:
        raise ResourceError("unreachable", str(exc)) from exc
    import json

    try:
        body = json.loads(raw or b"{}")
    except ValueError:
        body = {}
    if status >= 400:
        detail = str((body or {}).get("detail") or f"HTTP {status}")
        code, sep, _ = detail.partition(":")
        raise ResourceError(code.strip() if sep and code.strip().isidentifier() else "refused", detail)
    return int((body or {}).get("notified") or 0)

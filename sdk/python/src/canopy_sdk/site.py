"""The site's needs: what a connected site asks of canopy's agents, signed
(spec ``2026-10-22-canopy-site-agents-api-design`` §2) — the other half of the site agents API.

The site publishes ``site.json`` beside its ``jwks.json`` (the Django host
serves it at ``canopy/site.json``): its pages and the scopes each grants, what
an agent must be to work with its sensitive data, and which agents it actually
offers (and which of them it lets near that data).
canopy fetches it the way it fetches the site's keys and shows, per allowed
agent, whether the agent meets it — before anyone opens a page.

The pages and their scopes are the site's own description, for reference: canopy
shows them but never enforces them or tells an agent — an agent declares only
``sites:`` and ``data: sensitive``, and the site enforces its own page scopes.

It is a compact JWS signed with the site's key (``kid`` in its JWKS), ``typ``
``canopy-site+jwt``; ``iss`` is the site's Connected-site name::

    {"v": 1, "iss": "connect-labs", "iat": 1792700000,
     "pages": [{"key": "labs:workflow:run", "title": "A workflow run",
                "scopes": ["workflow:read", "workflow:act", "data:read"]}],
     "sensitive_data": {"requires": {"handles_sensitive": true, "runner_flag": "sensitive_data"}},
     "uses": {"agents": ["ace", "labsdata"], "sensitive_data_agents": ["labsdata"],
              "version": "<site agents version chosen against>"},
     "site": {"origins": ["https://labs.connect.dimagi.com"],
              "jwks_url": "https://labs.connect.dimagi.com/labs/canopy/jwks.json",
              "issuer": "https://labs.connect.dimagi.com",
              "mcp_resource": "https://labs.connect.dimagi.com/mcp/",
              "resource_schemes": ["labs-result"]}}

``site`` (optional) is the site's own facts — the values canopy's Connected
sites form otherwise has someone type: the origins that frame the panel, where
its keys are, its OAuth issuer and its MCP server. canopy never applies them on
its own: the site's owner in canopy reads them back ("Fill from the site") and
saves them, because a site must not be able to change its own trusted origins.
The Django host fills the block from settings it already has.

``site.resource_schemes`` (optional, SDK 0.18+) names the URI schemes of the
artifacts the site's tools link to (``canopy_sdk.resources``; the Django host fills
it from ``CANOPY_HOST["ARTIFACTS"]["SCHEME"]``). Unlike the rest of the block canopy
reads it LIVE, with no owner adopting it: it widens nothing — it is the list of
addresses canopy will keep from this site's own tool results (in a sensitive
conversation, the only thing it keeps), and a link under any other scheme is
dropped.

canopy finds the document beside the site's registered JWKS, or — knowing only
the site's address — at ``WELL_KNOWN_PATH`` on its origin (mount
``canopy_sdk.django.views.site_document`` there) or at ``canopy/site.json``
under the address it was given (the SDK's default mount).

Both halves are here: ``site_document`` / ``sign_site_document`` the site's,
``verify_site_document`` canopy's.
"""
from __future__ import annotations

import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from urllib.parse import urlsplit

from . import contract
from .contract import ContractError
from .jose import sign, unverified_header
from .keys import load_private_key, public_jwk

SITE_DOCUMENT_VERSION = 1
SITE_TYP = "canopy-site+jwt"
MEDIA_TYPE = "application/jose"
#: The file name, beside the site's ``jwks.json``.
SITE_DOCUMENT_NAME = "site.json"
#: Where a site that publishes its document at its origin's root serves it, so
#: canopy can find it from the site's address alone.
WELL_KNOWN_PATH = "/.well-known/canopy-site.json"
_JWKS_NAME = "jwks.json"


class SiteDocumentError(Exception):
    """The document could not be built or believed. ``code`` names why."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


def site_document_url(jwks_url: str) -> str:
    """Where a site publishes ``site.json``: beside its ``jwks.json``. ``""``
    when its JWKS URL does not end in ``jwks.json`` (no place to look)."""
    url = str(jwks_url or "").strip()
    head, sep, name = url.rpartition("/")
    if not sep or name != _JWKS_NAME:
        return ""
    return f"{head}/{SITE_DOCUMENT_NAME}"


def _strings(value, what: str) -> list[str]:
    if value is None:
        return []
    if not isinstance(value, (list, tuple)) or not all(isinstance(v, str) for v in value):
        raise SiteDocumentError("malformed", f"{what} must be a list of strings")
    return list(dict.fromkeys(v.strip() for v in value if v.strip()))


def _requires(value) -> dict:
    if value is None:
        return {}
    if not isinstance(value, Mapping):
        raise SiteDocumentError("malformed", "sensitive_data.requires must be a mapping")
    extra = set(value) - {"handles_sensitive", "runner_flag"}
    if extra:
        raise SiteDocumentError("malformed", f"sensitive_data.requires: unknown key(s) {sorted(extra)}")
    out: dict = {}
    if "handles_sensitive" in value:
        if not isinstance(value["handles_sensitive"], bool):
            raise SiteDocumentError("malformed", "sensitive_data.requires.handles_sensitive must be true or false")
        out["handles_sensitive"] = value["handles_sensitive"]
    flag = value.get("runner_flag")
    if flag:
        if flag not in contract.RUNNER_FLAGS:
            raise SiteDocumentError("malformed", f"sensitive_data.requires.runner_flag {flag!r} is not a "
                                                 f"runner flag canopy knows ({sorted(contract.RUNNER_FLAGS)})")
        out["runner_flag"] = flag
    return out


def _url(value, what: str) -> str:
    if value is None or value == "":
        return ""
    if not isinstance(value, str):
        raise SiteDocumentError("malformed", f"{what} must be a URL")
    value = value.strip()
    parts = urlsplit(value)
    if parts.scheme not in ("https", "http") or not parts.netloc:
        raise SiteDocumentError("malformed", f"{what} must be an http(s) URL, not {value!r}")
    return value


def _origin(value) -> str:
    url = _url(value, "site.origins[]").rstrip("/")
    parts = urlsplit(url)
    if parts.path or parts.query or parts.fragment:
        raise SiteDocumentError("malformed", f"site.origins: {value!r} is not an origin "
                                             "(scheme, host and optional port only)")
    return url


def _site(value) -> dict:
    """The ``site`` block, validated; ``{}`` when absent or empty."""
    if value is None:
        return {}
    if not isinstance(value, Mapping):
        raise SiteDocumentError("malformed", "site must be a mapping")
    extra = set(value) - set(SITE_FACTS)
    if extra:
        raise SiteDocumentError("malformed", f"site: unknown key(s) {sorted(extra)}")
    origins = value.get("origins") or []
    if not isinstance(origins, (list, tuple)):
        raise SiteDocumentError("malformed", "site.origins must be a list of origins")
    out = {"origins": list(dict.fromkeys(_origin(o) for o in origins if o)),
           "jwks_url": _url(value.get("jwks_url"), "site.jwks_url"),
           "issuer": _url(value.get("issuer"), "site.issuer"),
           "mcp_resource": _url(value.get("mcp_resource"), "site.mcp_resource"),
           "resource_schemes": _schemes(value.get("resource_schemes"))}
    if not out["resource_schemes"]:
        del out["resource_schemes"]
    return out if any(out.values()) else {}


def _schemes(value) -> list[str]:
    from .resources import ResourceError, check_scheme

    if value is None:
        return []
    if not isinstance(value, (list, tuple)):
        raise SiteDocumentError("malformed", "site.resource_schemes must be a list of URI schemes")
    try:
        return list(dict.fromkeys(check_scheme(v) for v in value if v))
    except ResourceError as exc:
        raise SiteDocumentError("malformed", f"site.resource_schemes: {exc.message}") from exc


#: The keys of ``site``, in order.
SITE_FACTS: tuple[str, ...] = ("origins", "jwks_url", "issuer", "mcp_resource", "resource_schemes")


def _normalize(claims: Mapping) -> dict:
    """Validated and in canonical shape; raises ``SiteDocumentError``."""
    pages = []
    for page in claims.get("pages") or []:
        if not isinstance(page, Mapping) or not str(page.get("key") or "").strip():
            raise SiteDocumentError("malformed", "every page needs a key")
        pages.append({"key": str(page["key"]).strip(), "title": str(page.get("title") or ""),
                      "scopes": _strings(page.get("scopes"), f"pages[{page['key']}].scopes")})
    sensitive = claims.get("sensitive_data") or {}
    if not isinstance(sensitive, Mapping):
        raise SiteDocumentError("malformed", "sensitive_data must be a mapping")
    uses = claims.get("uses") or {}
    if not isinstance(uses, Mapping):
        raise SiteDocumentError("malformed", "uses must be a mapping")
    agents = _strings(uses.get("agents"), "uses.agents")
    sensitive_agents = _strings(uses.get("sensitive_data_agents"), "uses.sensitive_data_agents")
    body = {"pages": pages, "sensitive_data": {"requires": _requires(sensitive.get("requires"))},
            "uses": {"agents": agents, "sensitive_data_agents": sensitive_agents,
                     "version": str(uses.get("version") or "")}}
    site = _site(claims.get("site"))
    if site:
        body["site"] = site
    return body


def site_document(site_name: str, *, pages: Sequence[Mapping] = (), sensitive_data: Mapping | None = None,
                  uses: Mapping | None = None, site: Mapping | None = None,
                  now: float | None = None) -> dict:
    """The payload, validated: ``pages`` as ``[{key, title, scopes}]``,
    ``sensitive_data`` as ``{"requires": {"handles_sensitive", "runner_flag"}}``, ``uses`` as
    ``{"agents", "sensitive_data_agents", "version"}``, ``site`` as ``{"origins",
    "jwks_url", "issuer", "mcp_resource", "resource_schemes"}`` (each optional; left out
    when empty)."""
    name = str(site_name or "").strip()
    if not name:
        raise SiteDocumentError("not_configured", "the document needs the site's Connected-site name")
    body = _normalize({"pages": list(pages), "sensitive_data": sensitive_data or {}, "uses": uses or {},
                       "site": site})
    return {"v": SITE_DOCUMENT_VERSION, "iss": name, "iat": int(time.time() if now is None else now),
            **body}


def sign_site_document(site_key, document: Mapping) -> str:
    key = load_private_key(site_key, what="the site signing key")
    return sign(dict(document), key, headers={"kid": public_jwk(key)["kid"], "typ": SITE_TYP})


@dataclass(frozen=True)
class SitePage:
    key: str
    title: str = ""
    scopes: tuple[str, ...] = ()


@dataclass(frozen=True)
class SiteFacts:
    """The ``site`` block: what the site says about itself, for canopy's owner to
    adopt (never applied without them)."""

    origins: tuple[str, ...] = ()
    jwks_url: str = ""
    issuer: str = ""
    mcp_resource: str = ""
    #: The URI schemes of the artifacts this site's tools link to (``canopy_sdk.resources``).
    resource_schemes: tuple[str, ...] = ()


@dataclass(frozen=True)
class SiteNeeds:
    """A site's verified ``site.json``."""

    site_name: str
    issued_at: int
    pages: tuple[SitePage, ...] = ()
    #: ``{"handles_sensitive": bool, "runner_flag": str}`` — what an agent must be to work
    #: with this site's sensitive data; empty when the site names nothing.
    sensitive_data_requires: Mapping = field(default_factory=dict)
    uses_agents: tuple[str, ...] = ()
    #: The agents the site lets work with its sensitive data.
    uses_sensitive_data_agents: tuple[str, ...] = ()
    #: The site agents version the site chose its settings against.
    uses_version: str = ""
    #: The site's own facts (its ``site`` block), or None when it declares none.
    site: SiteFacts | None = None
    claims: dict = field(repr=False, default_factory=dict)

    @classmethod
    def from_claims(cls, claims: Mapping, *, site_name: str = "") -> SiteNeeds:
        """Rebuild from VERIFIED claims (``verify_site_document``, or a cached
        copy of them). Raises ``SiteDocumentError`` on a shape it cannot read."""
        body = _normalize(claims)
        return cls(
            site_name=site_name or str(claims.get("iss") or ""), issued_at=int(claims.get("iat") or 0),
            pages=tuple(SitePage(p["key"], p["title"], tuple(p["scopes"])) for p in body["pages"]),
            sensitive_data_requires=body["sensitive_data"]["requires"],
            uses_agents=tuple(body["uses"]["agents"]),
            uses_sensitive_data_agents=tuple(body["uses"]["sensitive_data_agents"]),
            uses_version=body["uses"]["version"],
            site=(SiteFacts(origins=tuple(body["site"]["origins"]), jwks_url=body["site"]["jwks_url"],
                            issuer=body["site"]["issuer"], mcp_resource=body["site"]["mcp_resource"],
                            resource_schemes=tuple(body["site"].get("resource_schemes") or ()))
                  if body.get("site") else None),
            claims=dict(claims))


def verify_site_document(token: str, keys: Sequence, *, site_name: str,
                         algorithms: Sequence[str] = contract.ASSERTION_ALGORITHMS) -> SiteNeeds:
    """canopy's check of a site's ``site.json`` against the site's registered
    ``keys``: OUR algorithm list, ``typ``, the signature, ``iss`` = the site's
    name, ``v``, and the shape. Raises ``SiteDocumentError``."""
    import jwt

    try:
        header = unverified_header(token, algorithms=algorithms)
    except ContractError as exc:
        raise SiteDocumentError(exc.code, exc.message) from exc
    if header.get("typ") != SITE_TYP:
        raise SiteDocumentError("bad_typ", f"not a canopy site document (typ {SITE_TYP})")
    claims = None
    last: Exception | None = None
    for key in keys:
        try:
            claims = jwt.decode(token, key, algorithms=list(algorithms),
                                options={"require": ["v", "iss", "iat"], "verify_aud": False},
                                leeway=contract.LEEWAY_SECONDS)
            break
        except jwt.ExpiredSignatureError as exc:
            raise SiteDocumentError("expired", "the site document has expired") from exc
        except jwt.MissingRequiredClaimError as exc:
            raise SiteDocumentError("incomplete", f"the site document is missing {exc.claim!r}") from exc
        except Exception as exc:  # noqa: BLE001 - try the next key
            last = exc
    if claims is None:
        raise SiteDocumentError("bad_signature", f"no key registered for {site_name!r} verifies "
                                                 f"its site document ({last})")
    if not contract.constant_time_equal(str(claims.get("iss") or ""), site_name):
        raise SiteDocumentError("bad_issuer", f"the document is {claims.get('iss')!r}'s, not {site_name!r}'s")
    if claims.get("v") != SITE_DOCUMENT_VERSION:
        raise SiteDocumentError("bad_version", f"site document v{claims.get('v')!r} is not one canopy "
                                               f"reads (v{SITE_DOCUMENT_VERSION})")
    return SiteNeeds.from_claims(claims, site_name=site_name)

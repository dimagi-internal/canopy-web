"""The site's needs: what a host site asks of canopy's agents, signed — the other half of the site agents API.

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
``canopy-site+jwt``; ``iss`` is the site's host-site name::

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
              "resource_schemes": ["labs-result"],
              "resource_id_lengths": {"labs-result": 24}}}

``title`` (optional) is the site's display name ("Connect Labs"), and ``scope_labels`` (optional) says
in a short phrase what each scope lets an agent do ("read a round's responses and reviews"). They
exist for one reader: the person asked to let an agent use the site's page. canopy prints them in that
prompt, beside the raw scope names, as what the SITE says its own scopes cover; it never enforces
them. Both are descriptive text only: a missing one falls back to the site's name and the raw scopes.

``site`` (optional) is the site's own facts — the values canopy's Host
sites form otherwise has someone type: the origins that frame the panel, where
its keys are, its OAuth issuer and its MCP server. canopy never applies them on
its own: the site's owner in canopy reads them back ("Fill from the site") and
saves them, because a site must not be able to change its own trusted origins.
The Django host fills the block from settings it already has.

``site.resource_schemes`` (optional, SDK 0.18+) names the URI schemes of the
results the site's tools link to (``canopy_sdk.resources``; the Django host fills
it from ``CANOPY_HOST["RESOURCE_SCHEMES"]``). Unlike the rest of the block canopy
reads it LIVE, with no owner adopting it: it widens nothing — it is the list of
addresses canopy will keep from this site's own tool results (in a sensitive
conversation, the only thing it keeps), and a link under any other scheme is
dropped. ``site.resource_id_lengths`` (optional, SDK 0.22+; ``RESOURCE_ID_LENGTHS``)
says how long a scheme's result ids are: canopy then keeps a link under it only as
``<scheme>://`` and exactly that many url-safe characters. Without it an id may be up
to ``resources.MAX_ID_LENGTH`` of them.

``site.person_scheme`` (optional, SDK 0.30+; ``CANOPY_HOST["PERSON_SCHEME"]``) is the URI
scheme of the site's own records of its people (``labs-worker``): the site is the SYSTEM
OF RECORD for the people it brings, and a contact's ``record_uri``
(``canopy_sdk.contacts``) must be under it. ``site.retention_days`` (optional, SDK 0.30+;
``RETENTION_DAYS``) is how long canopy keeps the conversations of this site's contacts;
canopy enforces the shorter of it and the workspace's own rule. Both are read LIVE, like
``resource_schemes``: neither widens anything.

``site.reply_url`` (optional, SDK 0.38+; the Django host fills it from ``CANOPY_HOST["REPLY_URL"]``)
is where the site takes canopy's reply push (``canopy_sdk.host.replies``): the agent's replies in
the conversations the site relays over its own channel. ``site.reply_mode`` (optional;
``REPLY_MODE``) is ``final`` (one post when a turn ends) or ``stream`` (a post per piece of
prose). canopy ADOPTS a declared ``reply_url`` without its owner typing it, but only when the
document verifies against the site's registered keys, the URL is https, its origin is exactly one
of the site's REGISTERED origins (never the ones this document lists), and it passes canopy's
outbound checks: the site can name only an address it already holds. A reply webhook a person set
in canopy wins over the declared one.

``surfaces`` (optional, SDK 0.37+) splits one site into the parts that serve different people.
A site with none is one surface, as before. Each is keyed by a name (``SURFACE_NAME``) and says
whether its conversations hold the site's sensitive data (``sensitive``, REQUIRED, so a surface is
never non-sensitive by omission), the MCP resource a contact's subject-bound tool calls go to
(``mcp_resource``) and the scheme of its people's records (``person_scheme``)::

    "surfaces": {"workers": {"title": "Connect Worker messaging", "sensitive": false,
                             "mcp_resource": "https://labs.connect.dimagi.com/messaging/canopy/mcp/",
                             "person_scheme": "labs-worker"}}

An agent's host-site entry names the surface it serves (``surface: workers``). canopy lets a
site's contact hold a conversation on a surface the site declares ``sensitive: false`` (the
conversation is then not sensitive), and sends that contact's tool calls only to that surface's
resource, on the site's registered origin. The site's own pages stay the site itself, with
whatever sensitivity its visitor assertions require. Read LIVE, like ``person_scheme``.

canopy finds the document beside the site's registered JWKS, or — knowing only
the site's address — at ``WELL_KNOWN_PATH`` on its origin (mount
``canopy_sdk.django.views.site_document`` there) or at ``canopy/site.json``
under the address it was given (the SDK's default mount).

Both halves are here: ``site_document`` / ``sign_site_document`` the site's,
``verify_site_document`` canopy's.
"""
from __future__ import annotations

import re
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


#: A display name or scope label is shown to a person in a consent prompt: one short line.
MAX_TITLE = 80
MAX_LABEL = 160
MAX_LABELS = 64


def _text(value, what: str, limit: int) -> str:
    """One short line of descriptive text: no control characters, no newlines, bounded."""
    if value is None or value == "":
        return ""
    if not isinstance(value, str):
        raise SiteDocumentError("malformed", f"{what} must be text")
    text = " ".join(value.split())
    if any(ord(ch) < 32 or ord(ch) == 127 for ch in value.replace("\n", " ").replace("\t", " ")):
        raise SiteDocumentError("malformed", f"{what} must not contain control characters")
    if len(text) > limit:
        raise SiteDocumentError("malformed", f"{what} is longer than {limit} characters")
    return text


def _scope_labels(value) -> dict[str, str]:
    """``scope_labels``: ``{scope: phrase}`` — what each scope lets an agent do, in words."""
    if value is None:
        return {}
    if not isinstance(value, Mapping):
        raise SiteDocumentError("malformed", "scope_labels must map a scope to a phrase")
    if len(value) > MAX_LABELS:
        raise SiteDocumentError("malformed", f"scope_labels has more than {MAX_LABELS} scopes")
    out: dict[str, str] = {}
    for scope, label in value.items():
        key = str(scope).strip()
        if not key or any(ch.isspace() for ch in key):
            raise SiteDocumentError("malformed", f"scope_labels: {scope!r} is not a scope name")
        text = _text(label, f"scope_labels[{key}]", MAX_LABEL)
        if text:
            out[key] = text
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
    out["resource_id_lengths"] = _id_lengths(value.get("resource_id_lengths"), out["resource_schemes"])
    out["person_scheme"] = _person_scheme(value.get("person_scheme"))
    out["retention_days"] = _retention_days(value.get("retention_days"))
    out["reply_url"] = _url(value.get("reply_url"), "site.reply_url")
    out["reply_mode"] = _reply_mode(value.get("reply_mode"))
    for key in ("resource_schemes", "resource_id_lengths", "person_scheme", "retention_days",
                "reply_url", "reply_mode"):
        if not out[key]:
            del out[key]
    return out if any(out.values()) else {}


def _person_scheme(value) -> str:
    """``site.person_scheme``: the URI scheme of the site's records of its people
    (``labs-worker``), checked like a resource scheme (``resources.check_scheme``)."""
    from .resources import ResourceError, check_scheme

    if value is None or value == "":
        return ""
    if not isinstance(value, str):
        raise SiteDocumentError("malformed", "site.person_scheme must be a URI scheme")
    try:
        return check_scheme(value)
    except ResourceError as exc:
        raise SiteDocumentError("malformed", f"site.person_scheme: {exc.message}") from exc


#: The bounds on ``site.retention_days``: at least a day, at most ten years.
MIN_RETENTION_DAYS = 1
MAX_RETENTION_DAYS = 3650


def _retention_days(value) -> int | None:
    """``site.retention_days``: how many days canopy keeps the conversations of this site's
    contacts. canopy enforces the shorter of it and the workspace's own retention rule."""
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int) \
            or not MIN_RETENTION_DAYS <= value <= MAX_RETENTION_DAYS:
        raise SiteDocumentError("malformed", f"site.retention_days must be a whole number of days from "
                                             f"{MIN_RETENTION_DAYS} to {MAX_RETENTION_DAYS}")
    return value


#: The values of ``site.reply_mode``: one post when a turn ends, or one per piece of prose.
REPLY_MODES: tuple[str, ...] = ("final", "stream")


def _reply_mode(value) -> str:
    """``site.reply_mode``: ``final`` or ``stream``; "" when absent (canopy then posts ``final``)."""
    if value is None or value == "":
        return ""
    if value not in REPLY_MODES:
        raise SiteDocumentError("malformed", f"site.reply_mode must be one of {', '.join(REPLY_MODES)}, "
                                             f"not {value!r}")
    return value


def _id_lengths(value, schemes: list[str]) -> dict[str, int]:
    """``site.resource_id_lengths``: ``{scheme: n}`` — this scheme's result ids are exactly
    ``n`` url-safe characters (``canopy_sdk.resources.id_of``). Each scheme must be one of
    ``resource_schemes``."""
    from .resources import ResourceError, check_id_length, check_scheme

    if value is None:
        return {}
    if not isinstance(value, Mapping):
        raise SiteDocumentError("malformed", "site.resource_id_lengths must map a scheme to a length")
    out = {}
    try:
        for scheme, length in value.items():
            scheme = check_scheme(scheme)
            if scheme not in schemes:
                raise SiteDocumentError("malformed", f"site.resource_id_lengths: {scheme!r} is not "
                                                     "one of site.resource_schemes")
            out[scheme] = check_id_length(length)
    except ResourceError as exc:
        raise SiteDocumentError("malformed", f"site.resource_id_lengths: {exc.message}") from exc
    return dict(sorted(out.items()))


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


#: A surface's name: what an agent's host-site entry names (``surface:``).
SURFACE_NAME = re.compile(r"^[a-z][a-z0-9_-]{0,31}$")
#: At most this many surfaces per site.
MAX_SURFACES = 8
#: The keys a surface may carry.
SURFACE_KEYS: tuple[str, ...] = ("title", "sensitive", "mcp_resource", "person_scheme")


def _surfaces(value) -> dict:
    """``surfaces``: ``{name: {title?, sensitive, mcp_resource?, person_scheme?}}``, validated;
    ``{}`` when absent or empty. ``sensitive`` is required: a surface is never taken as not
    sensitive because a key was left out."""
    if value is None or value == {}:
        return {}
    if not isinstance(value, Mapping):
        raise SiteDocumentError("malformed", "surfaces must map a surface name to its facts")
    if len(value) > MAX_SURFACES:
        raise SiteDocumentError("malformed", f"surfaces has more than {MAX_SURFACES} entries")
    out: dict = {}
    for name, facts in value.items():
        if not isinstance(name, str) or not SURFACE_NAME.match(name):
            raise SiteDocumentError("malformed", f"surfaces: {name!r} is not a surface name "
                                                 f"({SURFACE_NAME.pattern})")
        if not isinstance(facts, Mapping):
            raise SiteDocumentError("malformed", f"surfaces.{name} must be a mapping")
        extra = set(facts) - set(SURFACE_KEYS)
        if extra:
            raise SiteDocumentError("malformed", f"surfaces.{name}: unknown key(s) {sorted(extra)}")
        if not isinstance(facts.get("sensitive"), bool):
            raise SiteDocumentError("malformed", f"surfaces.{name}.sensitive must be true or false")
        entry = {"sensitive": facts["sensitive"]}
        title = _text(facts.get("title"), f"surfaces.{name}.title", MAX_TITLE)
        if title:
            entry["title"] = title
        resource = _url(facts.get("mcp_resource"), f"surfaces.{name}.mcp_resource")
        if resource:
            entry["mcp_resource"] = resource
        scheme = _person_scheme(facts.get("person_scheme"))
        if scheme:
            entry["person_scheme"] = scheme
        out[name] = entry
    return dict(sorted(out.items()))


#: The keys of ``site``, in order.
SITE_FACTS: tuple[str, ...] = ("origins", "jwks_url", "issuer", "mcp_resource", "resource_schemes",
                               "resource_id_lengths", "person_scheme", "retention_days", "reply_url",
                               "reply_mode")


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
    title = _text(claims.get("title"), "title", MAX_TITLE)
    if title:
        body["title"] = title
    labels = _scope_labels(claims.get("scope_labels"))
    if labels:
        body["scope_labels"] = labels
    site = _site(claims.get("site"))
    if site:
        body["site"] = site
    surfaces = _surfaces(claims.get("surfaces"))
    if surfaces:
        body["surfaces"] = surfaces
    return body


def site_document(site_name: str, *, pages: Sequence[Mapping] = (), sensitive_data: Mapping | None = None,
                  uses: Mapping | None = None, site: Mapping | None = None,
                  title: str = "", scope_labels: Mapping | None = None,
                  surfaces: Mapping | None = None, now: float | None = None) -> dict:
    """The payload, validated: ``pages`` as ``[{key, title, scopes}]``,
    ``sensitive_data`` as ``{"requires": {"handles_sensitive", "runner_flag"}}``, ``uses`` as
    ``{"agents", "sensitive_data_agents", "version"}``, ``site`` as ``{"origins",
    "jwks_url", "issuer", "mcp_resource", "resource_schemes", "resource_id_lengths", "person_scheme",
    "retention_days", "reply_url", "reply_mode"}`` (each optional; left out
    when empty), ``title`` the site's display name and ``scope_labels`` as ``{scope: phrase}`` (both optional,
    descriptive text for the consent prompt), and ``surfaces`` as ``{name: {"sensitive", "mcp_resource",
    "person_scheme", "title"}}`` (optional; ``sensitive`` required on each)."""
    name = str(site_name or "").strip()
    if not name:
        raise SiteDocumentError("not_configured", "the document needs the site's host-site name")
    body = _normalize({"pages": list(pages), "sensitive_data": sensitive_data or {}, "uses": uses or {},
                       "site": site, "title": title, "scope_labels": scope_labels,
                       "surfaces": surfaces})
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
    #: The URI schemes of the results this site's tools link to (``canopy_sdk.resources``).
    resource_schemes: tuple[str, ...] = ()
    #: ``((scheme, n), ...)``: the exact length of a scheme's result ids, where declared.
    resource_id_lengths: tuple[tuple[str, int], ...] = ()
    #: The URI scheme of the site's own records of its people (``labs-worker``): a contact's
    #: ``record_uri`` must be under it (``canopy_sdk.contacts``). "" when none is declared.
    person_scheme: str = ""
    #: How many days canopy keeps the conversations of this site's contacts (the shorter of
    #: this and the workspace's own rule wins). None when the site declares none.
    retention_days: int | None = None
    #: Where the site takes canopy's reply push ("" when it declares none), and how
    #: (``final`` / ``stream``; "" when it declares none).
    reply_url: str = ""
    reply_mode: str = ""

    def resource_policy(self) -> dict[str, int | None]:
        """``{scheme: declared id length or None}``, as ``resources.validate_link`` takes it."""
        lengths = dict(self.resource_id_lengths)
        return {scheme: lengths.get(scheme) for scheme in self.resource_schemes}


@dataclass(frozen=True)
class SiteSurface:
    """One surface of a site (``surfaces``): a part of it serving its own people."""

    name: str
    #: Whether its conversations hold the site's sensitive data. A contact may hold a
    #: conversation only on a surface that does not.
    sensitive: bool
    #: Where a contact's subject-bound tool calls on this surface go ("" for none).
    mcp_resource: str = ""
    #: The scheme of the records of this surface's people ("" for none declared).
    person_scheme: str = ""
    title: str = ""


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
    #: The site's display name ("" when it publishes none): canopy's consent prompt says it.
    title: str = ""
    #: ``{scope: phrase}`` — what the site says each scope lets an agent do. Descriptive only.
    scope_labels: Mapping = field(default_factory=dict)
    #: The site's surfaces (``surfaces``), by name; empty when it declares none (the site is
    #: then its own single surface).
    surfaces: tuple[SiteSurface, ...] = ()
    claims: dict = field(repr=False, default_factory=dict)

    def surface(self, name: str) -> SiteSurface | None:
        """The surface called ``name``, or None when the site declares no such surface."""
        return next((s for s in self.surfaces if s.name == name), None)

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
                            resource_schemes=tuple(body["site"].get("resource_schemes") or ()),
                            resource_id_lengths=tuple(
                                (body["site"].get("resource_id_lengths") or {}).items()),
                            person_scheme=body["site"].get("person_scheme") or "",
                            retention_days=body["site"].get("retention_days"),
                            reply_url=body["site"].get("reply_url") or "",
                            reply_mode=body["site"].get("reply_mode") or "")
                  if body.get("site") else None),
            title=body.get("title", ""), scope_labels=dict(body.get("scope_labels") or {}),
            surfaces=tuple(SiteSurface(name=name, sensitive=facts["sensitive"],
                                       mcp_resource=facts.get("mcp_resource", ""),
                                       person_scheme=facts.get("person_scheme", ""),
                                       title=facts.get("title", ""))
                           for name, facts in (body.get("surfaces") or {}).items()),
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

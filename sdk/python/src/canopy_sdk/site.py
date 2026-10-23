"""The site's needs: what a connected site asks of canopy's agents, signed
(spec ``2026-10-22-canopy-site-agents-api-design`` §2) — the other half of the site agents API.

The site publishes ``site.json`` beside its ``jwks.json`` (the Django host
serves it at ``canopy/site.json``): its pages and the scopes each grants, what
an agent must be to read real data there, and which agents it actually offers.
canopy fetches it the way it fetches the site's keys and shows, per allowed
agent, whether the agent meets it — before anyone opens a page.

It is a compact JWS signed with the site's key (``kid`` in its JWKS), ``typ``
``canopy-site+jwt``; ``iss`` is the site's Connected-site name::

    {"v": 1, "iss": "connect-labs", "iat": 1792700000,
     "pages": [{"key": "labs:workflow:run", "title": "A workflow run",
                "scopes": ["workflow:read", "workflow:act", "data:read"]}],
     "real_data": {"requires": {"handles_sensitive": true, "runner_flag": "sensitive_data"}},
     "uses": {"agents": ["ace", "labsdata"], "real_data_agents": ["labsdata"],
              "version": "<site agents version chosen against>"}}

Both halves are here: ``site_document`` / ``sign_site_document`` the site's,
``verify_site_document`` canopy's.
"""
from __future__ import annotations

import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field

from . import contract
from .contract import ContractError
from .jose import sign, unverified_header
from .keys import load_private_key, public_jwk

SITE_DOCUMENT_VERSION = 1
SITE_TYP = "canopy-site+jwt"
MEDIA_TYPE = "application/jose"
#: The file name, beside the site's ``jwks.json``.
SITE_DOCUMENT_NAME = "site.json"
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
        raise SiteDocumentError("malformed", "real_data.requires must be a mapping")
    extra = set(value) - {"handles_sensitive", "runner_flag"}
    if extra:
        raise SiteDocumentError("malformed", f"real_data.requires: unknown key(s) {sorted(extra)}")
    out: dict = {}
    if "handles_sensitive" in value:
        if not isinstance(value["handles_sensitive"], bool):
            raise SiteDocumentError("malformed", "real_data.requires.handles_sensitive must be true or false")
        out["handles_sensitive"] = value["handles_sensitive"]
    flag = value.get("runner_flag")
    if flag:
        if flag not in contract.RUNNER_FLAGS:
            raise SiteDocumentError("malformed", f"real_data.requires.runner_flag {flag!r} is not a "
                                                 f"runner flag canopy knows ({sorted(contract.RUNNER_FLAGS)})")
        out["runner_flag"] = flag
    return out


def _normalize(claims: Mapping) -> dict:
    """Validated and in canonical shape; raises ``SiteDocumentError``."""
    pages = []
    for page in claims.get("pages") or []:
        if not isinstance(page, Mapping) or not str(page.get("key") or "").strip():
            raise SiteDocumentError("malformed", "every page needs a key")
        pages.append({"key": str(page["key"]).strip(), "title": str(page.get("title") or ""),
                      "scopes": _strings(page.get("scopes"), f"pages[{page['key']}].scopes")})
    real = claims.get("real_data") or {}
    if not isinstance(real, Mapping):
        raise SiteDocumentError("malformed", "real_data must be a mapping")
    uses = claims.get("uses") or {}
    if not isinstance(uses, Mapping):
        raise SiteDocumentError("malformed", "uses must be a mapping")
    agents = _strings(uses.get("agents"), "uses.agents")
    real_agents = _strings(uses.get("real_data_agents"), "uses.real_data_agents")
    return {"pages": pages, "real_data": {"requires": _requires(real.get("requires"))},
            "uses": {"agents": agents, "real_data_agents": real_agents,
                     "version": str(uses.get("version") or "")}}


def site_document(site_name: str, *, pages: Sequence[Mapping] = (), real_data: Mapping | None = None,
                  uses: Mapping | None = None, now: float | None = None) -> dict:
    """The payload, validated: ``pages`` as ``[{key, title, scopes}]``,
    ``real_data`` as ``{"requires": {"handles_sensitive", "runner_flag"}}``, ``uses`` as
    ``{"agents", "real_data_agents", "version"}``."""
    name = str(site_name or "").strip()
    if not name:
        raise SiteDocumentError("not_configured", "the document needs the site's Connected-site name")
    body = _normalize({"pages": list(pages), "real_data": real_data or {}, "uses": uses or {}})
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
class SiteNeeds:
    """A site's verified ``site.json``."""

    site_name: str
    issued_at: int
    pages: tuple[SitePage, ...] = ()
    #: ``{"handles_sensitive": bool, "runner_flag": str}`` — what an agent must be to read
    #: real data here; empty when the site names nothing.
    real_data_requires: Mapping = field(default_factory=dict)
    uses_agents: tuple[str, ...] = ()
    uses_real_data_agents: tuple[str, ...] = ()
    #: The site agents version the site chose its settings against.
    uses_version: str = ""
    claims: dict = field(repr=False, default_factory=dict)

    @classmethod
    def from_claims(cls, claims: Mapping, *, site_name: str = "") -> SiteNeeds:
        """Rebuild from VERIFIED claims (``verify_site_document``, or a cached
        copy of them). Raises ``SiteDocumentError`` on a shape it cannot read."""
        body = _normalize(claims)
        return cls(
            site_name=site_name or str(claims.get("iss") or ""), issued_at=int(claims.get("iat") or 0),
            pages=tuple(SitePage(p["key"], p["title"], tuple(p["scopes"])) for p in body["pages"]),
            real_data_requires=body["real_data"]["requires"],
            uses_agents=tuple(body["uses"]["agents"]),
            uses_real_data_agents=tuple(body["uses"]["real_data_agents"]),
            uses_version=body["uses"]["version"], claims=dict(claims))


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

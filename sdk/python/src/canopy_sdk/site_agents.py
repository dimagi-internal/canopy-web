"""The site agents API: canopy tells a connected site, signed, which agents it
may show and on what terms (spec ``2026-10-22-canopy-site-agents-api-design`` §1).

A site used to keep hand-typed copies of canopy's facts — agent slugs, which
of them are PHI-safe, the runner flags canopy knows — and nothing checked the
copies until a visitor's call reached the site and an agent silently did not
appear. This API replaces the copies: the site asks, canopy answers with one
signed document, and the site's settings are CHOSEN from it.

No new secrets. Both trust anchors already exist:

* the site asks with a JWT signed by ITS key (the one at its ``jwks_url``,
  exactly what signs its visitor assertions), so canopy knows which site is
  asking and answers for that site only;
* canopy answers with a JWS signed by ITS client key — the ``Canopy-Execution``
  key, found through canopy's Client ID Metadata Document — so the site
  believes it exactly as it believes a ``Canopy-Execution`` statement.

    from canopy_sdk.host import ClientKeyResolver
    from canopy_sdk.site_agents import SiteAgentsError, fetch_site_agents

    try:
        listing = fetch_site_agents("https://canopy.dimagi.com", "connect-labs", signing_key,
                                    ClientKeyResolver())
    except SiteAgentsError as exc:
        ...                                   # exc.code: unreachable, bad_signature...
    agent = listing.agent("labsdata")
    if agent and agent.ready and agent.phi_safe and agent.ready_with_flag("zdr"):
        ...

A Django host reads ``canopy_sdk.django.site_agents.current()`` instead: cached,
refreshed on a miss, the last good copy served (marked ``stale``) for up to a
day when canopy cannot be reached, then None — show no agent.

Both halves live here so canopy and every site read ONE definition:
``request_assertion`` / ``verify_site_agents`` / ``fetch_site_agents`` are the site's,
``verify_request`` / ``sign_site_agents`` / ``content_version`` canopy's.
"""
from __future__ import annotations

import hashlib
import json
import time
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field, replace

from . import contract
from .contract import ContractError, constant_time_equal, normalize_url
from .jose import decode, new_jti, sign, unverified_header
from .keys import load_private_key, public_jwk, public_key_for

#: Where canopy answers, under its base URL.
PATH = "/api/sites/agents"
#: The payload's ``v``. A site refuses any other: a shape it was not built for
#: is not one it can choose settings from.
PAYLOAD_VERSION = 1
#: ``typ`` of the site's request, and of canopy's answer. Distinct from every
#: other token either side signs, so neither can be replayed as the other.
REQUEST_TYP = "canopy-site-agents-request+jwt"
RESPONSE_TYP = "canopy-site-agents+jwt"
#: The answer's media type.
MEDIA_TYPE = "application/jose"
#: The request is used once, at the moment of the GET.
REQUEST_MAX_LIFETIME = 60
#: How long canopy's answer lives. A site caches it for less (5 minutes).
LIFETIME = 900
#: The longest answer a site accepts: a year-long answer would be a standing copy.
MAX_LIFETIME = 3600
REQUEST_REQUIRED_CLAIMS: tuple[str, ...] = ("iss", "sub", "aud", "iat", "exp", "jti")
RESPONSE_REQUIRED_CLAIMS: tuple[str, ...] = ("v", "iss", "aud", "iat", "exp", "version", "agents")
#: What ``version`` is a digest of: everything in the payload bar who, to whom,
#: and when. It changes exactly when one of these does.
VERSIONED_KEYS: tuple[str, ...] = ("site", "runner_flags", "agents")


class SiteAgentsError(Exception):
    """The site agents response could not be had or believed. ``code`` names why; the message
    never contains a credential."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


def endpoint_url(canopy_url: str) -> str:
    """The site agents endpoint of the canopy at ``canopy_url`` — the request's ``aud``."""
    return normalize_url(canopy_url) + PATH


# --- the site asks -----------------------------------------------------------------


def request_assertion(site_name: str, site_key, audience: str, *, agent: str = "",
                      now: float | None = None) -> str:
    """The site's signed request for its agents.

    ``iss`` = ``sub`` = the site's Connected-site name, ``aud`` = the endpoint URL
    (``endpoint_url``), 60 seconds, single-use ``jti``. ``agent`` names one of the
    site's agents, which tells canopy WHICH tenant's registration of
    ``site_name`` is meant — needed only when two tenants registered the same
    name (``ambiguous_issuer`` otherwise)."""
    name = str(site_name or "").strip()
    if not name:
        raise ValueError("the request needs the site's Connected-site name")
    key = load_private_key(site_key, what="the site signing key")
    issued = int(time.time() if now is None else now)
    claims = {"iss": name, "sub": name, "aud": audience, "iat": issued,
              "exp": issued + REQUEST_MAX_LIFETIME, "jti": new_jti()}
    agent = contract.parse_agent_claim(agent or None)
    if agent:
        claims[contract.AGENT_CLAIM] = agent
    return sign(claims, key, headers={"kid": public_jwk(key)["kid"], "typ": REQUEST_TYP})


# --- canopy answers ----------------------------------------------------------------


def verify_request(token: str, keys: Sequence, *, audiences: Iterable[str],
                   spend_jti: Callable[[dict], None] | None = None,
                   algorithms: Sequence[str] = contract.ASSERTION_ALGORITHMS,
                   leeway: int = contract.LEEWAY_SECONDS) -> dict:
    """canopy's check of a site's request against that site's ``keys``. Returns
    its claims or raises ``SiteAgentsError``.

    Strict like every verifier here: OUR algorithm list, ``typ``, ``aud`` one of
    ``audiences`` (canopy's endpoint URL under each address it answers on), ``exp``
    required and the life capped at 60s, ``sub`` = ``iss`` (a site speaks only
    for itself), and ``spend_jti(claims)`` — the caller's single-use store —
    before anything is returned."""
    import jwt

    try:
        header = unverified_header(token, algorithms=algorithms)
    except ContractError as exc:
        raise SiteAgentsError(exc.code, exc.message) from exc
    if header.get("typ") != REQUEST_TYP:
        raise SiteAgentsError("bad_typ", f"a site agents request must have typ {REQUEST_TYP}")
    wanted = [a for a in (normalize_url(x) for x in audiences) if a]
    claims = None
    last: Exception | None = None
    for key in keys:
        try:
            claims = jwt.decode(token, key, algorithms=list(algorithms), audience=wanted,
                                leeway=leeway,
                                options={"require": list(REQUEST_REQUIRED_CLAIMS),
                                         "verify_aud": True, "verify_exp": True})
            break
        except jwt.InvalidAudienceError as exc:
            raise SiteAgentsError("wrong_audience", "the request is addressed to a different canopy") from exc
        except jwt.ExpiredSignatureError as exc:
            raise SiteAgentsError("expired", "the request has expired") from exc
        except jwt.MissingRequiredClaimError as exc:
            raise SiteAgentsError("incomplete", f"the request is missing {exc.claim!r}") from exc
        except Exception as exc:  # noqa: BLE001 - try the next key
            last = exc
    if claims is None:
        raise SiteAgentsError("bad_signature", f"no key registered for this site verifies the request ({last})")
    try:
        contract.check_lifetime(claims, REQUEST_MAX_LIFETIME, leeway=leeway, what="a site agents request")
        contract.check_jti(claims)
    except ContractError as exc:
        raise SiteAgentsError(exc.code, exc.message) from exc
    if not constant_time_equal(str(claims.get("sub") or ""), str(claims.get("iss") or "")):
        raise SiteAgentsError("bad_subject", "a site asks for its own agents only (sub must equal iss)")
    if spend_jti is not None:
        spend_jti(claims)
    return claims


def canonical(value) -> bytes:
    """The one serialisation ``version`` is computed over: sorted keys, no
    whitespace, UTF-8. Any implementation can reproduce it."""
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()


def content_version(content: Mapping) -> str:
    """sha256 (hex) of the canonical ``site`` + ``runner_flags`` + ``agents``."""
    return hashlib.sha256(canonical({k: content.get(k) for k in VERSIONED_KEYS})).hexdigest()


def sign_site_agents(key, client_id: str, site_name: str, content: Mapping, *,
               now: float | None = None, lifetime: int = LIFETIME) -> str:
    """canopy's answer: ``content`` (``site``, ``runner_flags``, ``agents``)
    with ``v``, ``iss`` = canopy's client_id, ``aud`` = the site's name,
    ``iat``/``exp`` and ``version``, signed by canopy's client key."""
    if not 0 < lifetime <= MAX_LIFETIME:
        raise ValueError(f"a site agents response lives 1..{MAX_LIFETIME}s")
    issued = int(time.time() if now is None else now)
    body = {k: content.get(k) for k in VERSIONED_KEYS}
    claims = {"v": PAYLOAD_VERSION, "iss": client_id, "aud": site_name, "iat": issued,
              "exp": issued + lifetime, "version": content_version(body), **body}
    return sign(claims, key, headers={"kid": public_jwk(key)["kid"], "typ": RESPONSE_TYP})


# --- the site reads ----------------------------------------------------------------


def _strs(value) -> tuple[str, ...]:
    return tuple(str(v) for v in value) if isinstance(value, (list, tuple)) else ()


def _maps(value) -> list[Mapping]:
    return [v for v in value if isinstance(v, Mapping)] if isinstance(value, (list, tuple)) else []


@dataclass(frozen=True)
class RunnerFlag:
    """A runner flag canopy knows, and what it means."""

    flag: str
    meaning: str = ""


@dataclass(frozen=True)
class AgentCapability:
    """One capability of a listed agent that names this site in ``sites:``."""

    name: str
    description: str = ""
    #: Caller classes the capability is published for (``contact``, ``member``…).
    callers: tuple[str, ...] = ()
    #: The capability is ``data: phi`` on the agent's published interface.
    phi_safe: bool = False
    #: Who published the interface holding the setting, and when ("" when not PHI-safe).
    phi_set_by: str = ""
    phi_set_at: str = ""

    @classmethod
    def from_mapping(cls, value: Mapping) -> AgentCapability:
        return cls(name=str(value.get("name") or ""), description=str(value.get("description") or ""),
                   callers=_strs(value.get("callers")), phi_safe=value.get("phi_safe") is True,
                   phi_set_by=str(value.get("phi_set_by") or ""),
                   phi_set_at=str(value.get("phi_set_at") or ""))


@dataclass(frozen=True)
class SiteAgent:
    """One agent canopy allows on this site, and whether it can serve it now."""

    slug: str
    name: str = ""
    description: str = ""
    workspace: str = ""
    owner: str = ""
    url: str = ""
    capabilities: tuple[AgentCapability, ...] = ()
    #: Flags carried by at least one runner the agent may run on.
    flags_available: tuple[str, ...] = ()
    #: ``{flag: bool}`` — a runner carrying the flag is ready now.
    ready_with: Mapping[str, bool] = field(default_factory=dict)
    ready: bool = False
    #: Plain sentences, each naming where in canopy to fix it.
    problems: tuple[str, ...] = ()

    @property
    def phi_safe(self) -> bool:
        """Some capability for this site is PHI-safe."""
        return any(c.phi_safe for c in self.capabilities)

    def ready_with_flag(self, flag: str) -> bool:
        return self.ready_with.get(flag) is True

    def capability(self, name: str) -> AgentCapability | None:
        return next((c for c in self.capabilities if c.name == name), None)

    @classmethod
    def from_mapping(cls, value: Mapping) -> SiteAgent:
        runners = value.get("runners") if isinstance(value.get("runners"), Mapping) else {}
        ready_with = runners.get("ready_with") if isinstance(runners.get("ready_with"), Mapping) else {}
        return cls(
            slug=str(value.get("slug") or ""), name=str(value.get("name") or ""),
            description=str(value.get("description") or ""),
            workspace=str(value.get("workspace") or ""), owner=str(value.get("owner") or ""),
            url=str(value.get("url") or ""),
            capabilities=tuple(AgentCapability.from_mapping(c) for c in _maps(value.get("capabilities"))),
            flags_available=_strs(runners.get("flags_available")),
            ready_with={str(k): v is True for k, v in ready_with.items()},
            ready=value.get("ready") is True, problems=_strs(value.get("problems")))


@dataclass(frozen=True)
class SiteAgents:
    """canopy's verified answer: the agents this site may show.

    ``stale`` is True when it is a last good copy served because canopy could
    not be reached; ``age`` is seconds since it was fetched."""

    version: str
    site_name: str
    tenant: str
    agents: tuple[SiteAgent, ...]
    runner_flags: tuple[RunnerFlag, ...]
    issuer: str
    issued_at: int
    expires_at: int
    claims: dict = field(repr=False, default_factory=dict)
    stale: bool = False
    age: float = 0.0

    def agent(self, slug: str) -> SiteAgent | None:
        return next((a for a in self.agents if a.slug == slug), None)

    @property
    def slugs(self) -> tuple[str, ...]:
        return tuple(a.slug for a in self.agents)

    def flag(self, name: str) -> RunnerFlag | None:
        return next((f for f in self.runner_flags if f.flag == name), None)

    def aged(self, age: float, *, stale: bool = False) -> SiteAgents:
        return replace(self, age=max(0.0, float(age)), stale=stale)

    @classmethod
    def from_claims(cls, claims: Mapping, *, stale: bool = False, age: float = 0.0) -> SiteAgents:
        """Rebuild from VERIFIED claims (``verify_site_agents``, or a cached copy of them)."""
        site = claims.get("site") if isinstance(claims.get("site"), Mapping) else {}
        return cls(
            version=str(claims.get("version") or ""),
            site_name=str(site.get("name") or claims.get("aud") or ""),
            tenant=str(site.get("tenant") or ""),
            agents=tuple(SiteAgent.from_mapping(a) for a in _maps(claims.get("agents"))),
            runner_flags=tuple(RunnerFlag(str(f.get("flag") or ""), str(f.get("meaning") or ""))
                               for f in _maps(claims.get("runner_flags"))),
            issuer=str(claims.get("iss") or ""), issued_at=int(claims.get("iat") or 0),
            expires_at=int(claims.get("exp") or 0), claims=dict(claims), stale=stale,
            age=max(0.0, float(age)))


def verify_site_agents(token: str, *, client_id: str, site_name: str, client_keys) -> SiteAgents:
    """Check canopy's answer and return the ``SiteAgents``; ``SiteAgentsError`` otherwise.

    ``client_keys`` is a ``canopy_sdk.host.ClientKeyResolver`` (the same one that
    verifies ``Canopy-Execution``). Checked, all fail-closed: ``typ``, the
    signature against the key canopy's client metadata names (``kid``), ``iss`` =
    ``client_id``, ``aud`` = this site, ``exp`` (and a life of at most an hour),
    ``v``, and that ``version`` is the digest of what it covers."""
    from .host.client_keys import MetadataError

    try:
        header = unverified_header(token)
        if header.get("typ") != RESPONSE_TYP:
            raise ContractError("bad_typ", f"not a canopy site agents response (typ {RESPONSE_TYP})")
        jwk = client_keys.key_for(client_id, header.get("kid"))
        key = public_key_for(jwk, header["alg"])
        claims = decode(token, key, algorithms=[header["alg"]], audience=[site_name],
                        required=RESPONSE_REQUIRED_CLAIMS, leeway=contract.LEEWAY_SECONDS)
        contract.check_lifetime(claims, MAX_LIFETIME, what="a site agents response")
    except MetadataError as exc:
        raise SiteAgentsError("client_metadata", f"canopy's keys could not be read: {exc}") from exc
    except ContractError as exc:
        raise SiteAgentsError(exc.code, exc.message) from exc
    if not constant_time_equal(str(claims.get("iss") or ""), client_id):
        raise SiteAgentsError("bad_issuer", "the response is not from the canopy this site trusts")
    if claims.get("v") != PAYLOAD_VERSION:
        raise SiteAgentsError("bad_version", f"site agents v{claims.get('v')!r} is not one this SDK reads "
                                        f"(v{PAYLOAD_VERSION}); upgrade dimagi-canopy")
    if not isinstance(claims.get("agents"), list):
        raise SiteAgentsError("malformed", "the response lists no agents")
    if not constant_time_equal(str(claims.get("version") or ""), content_version(claims)):
        raise SiteAgentsError("bad_content_version", "the response's version is not the digest of its content")
    return SiteAgents.from_claims(claims)


def fetch_site_agents(canopy_url: str, site_name: str, site_key, canopy_keys, *,
                client_id: str | None = None, agent: str = "",
                get: Callable[..., str] | None = None) -> SiteAgents:
    """Ask canopy which agents this site may show, and verify the answer.

    ``site_key`` signs the request (the site's own key, the one its ``jwks_url``
    publishes); ``canopy_keys`` is a ``ClientKeyResolver`` for canopy's client
    key. ``client_id`` defaults to canopy's at ``canopy_url`` — pass the host
    config's ``canopy_client_id`` when canopy's identity is not its address.
    The GET goes through ``canopy_sdk.fetch`` (https only, address-pinned,
    bounded, no redirects). Raises ``SiteAgentsError``."""
    from . import fetch

    url = endpoint_url(canopy_url)
    try:
        token = request_assertion(site_name, site_key, url, agent=agent)
    except (ValueError, ContractError) as exc:
        raise SiteAgentsError("not_configured", str(exc)) from exc
    getter = get or fetch.get_text
    try:
        body = getter(url, headers={"Authorization": f"Bearer {token}"}, accept=MEDIA_TYPE)
    except fetch.FetchError as exc:
        raise SiteAgentsError("unreachable", str(exc)) from exc
    return verify_site_agents(body, client_id=client_id or contract.client_id_for(canopy_url),
                        site_name=site_name, client_keys=canopy_keys)

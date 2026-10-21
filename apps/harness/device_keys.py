"""Runner device keys and the DPoP device proof (Canopy Vault, canopy-web#1538 Phase 1a).

A runner registers the PUBLIC half of an EC P-256 key it holds (ideally in the
Secure Enclave or a TPM). Once a person approves it, a request to
`/credentials/resolve` can carry an RFC 9449 DPoP proof signed by that key, and
for an agent with `require_device_binding` it must.

The proof (the contract both halves build against):

* a compact JWS in the `DPoP` header, `typ: dpop+jwt`, `alg: ES256`, the public
  `jwk` in its header;
* claims `htm` (the method), `htu` (scheme + host + path, no query), `iat` within
  ±120 s, and a `jti` used once.

Verification reuses canopy_sdk's JOSE building blocks (the same ones the host
grant contract uses), with this contract's algorithm list (ES256 only) and its
wider ±120 s window. The `jti` is consumed in the cache, so a replayed proof is
refused inside the window it would otherwise be fresh for.
"""
from __future__ import annotations

import time
from urllib.parse import urlsplit

from django.conf import settings
from django.core.cache import cache
from django.utils import timezone

ALG = "ES256"
IAT_WINDOW_SECONDS = 120
#: How long a seen jti is remembered: the whole window it could be fresh in,
#: both sides of now, plus slack.
JTI_TTL_SECONDS = 2 * IAT_WINDOW_SECONDS + 30
PROOF_HEADER = "HTTP_DPOP"


class ProofError(ValueError):
    """The proof is missing, malformed, stale, replayed, or for another request."""


class KeyError_(ValueError):
    """A public JWK that is not an EC P-256 public key."""


def normalize_public_jwk(jwk) -> dict:
    """The JWK reduced to its RFC 7638 required members, after checking it is a
    valid P-256 PUBLIC key (on the curve, no private members)."""
    from canopy_sdk.contract import ContractError
    from canopy_sdk.keys import public_key_for

    if not isinstance(jwk, dict):
        raise KeyError_("public_jwk must be a JWK object")
    if jwk.get("kty") != "EC" or jwk.get("crv") != "P-256":
        raise KeyError_("public_jwk must be an EC P-256 key")
    try:
        public_key_for(jwk, ALG, algorithms=(ALG,))
    except ContractError as exc:
        raise KeyError_(f"public_jwk is not a usable P-256 public key: {exc}") from exc
    return {k: jwk[k] for k in ("crv", "kty", "x", "y")}


def thumbprint(jwk: dict) -> str:
    from canopy_sdk.contract import jwk_thumbprint

    return jwk_thumbprint(jwk)


def _strip_to_flat(path: str) -> str:
    """The path as the flat API route sees it: no legacy `/canopy` prefix, no
    `/w/<workspace>` tenant segment (WorkspaceResolveMiddleware strips it on the
    way in, so `request.path` never has it), no trailing slash."""
    prefix = (getattr(settings, "CANOPY_STRIP_PREFIX", "") or "/canopy").rstrip("/")
    if prefix and (path == prefix or path.startswith(prefix + "/")):
        path = path[len(prefix):]
    if path.startswith("/api/w/"):
        rest = path[len("/api/w/"):]
        _, _, tail = rest.partition("/")
        path = "/api/" + tail
    return path.rstrip("/")


def htu_matches(htu: str, request) -> bool:
    """Does the proof's `htu` name THIS endpoint on a host that serves canopy?

    Behind the load balancer the request's own URL is not what the client
    signed: the old address arrives with a `/canopy` prefix that is stripped
    before Django, and a tenant URL's `/w/<ws>` is rewritten to the flat route.
    So the check is the parts that bind the proof — an https (or, locally, http)
    URL, a host this deployment answers to, and the same flat path."""
    parts = urlsplit(htu or "")
    if parts.query or parts.fragment or parts.scheme not in ("https", "http"):
        return False
    host = (parts.hostname or "").lower()
    allowed = {h.lower() for h in getattr(settings, "ALLOWED_HOSTS", []) if h and h != "*"}
    allowed.add(request.get_host().split(":")[0].lower())
    base = urlsplit(getattr(settings, "CANOPY_PUBLIC_BASE_URL", "") or "").hostname
    if base:
        allowed.add(base.lower())
    if host not in allowed:
        return False
    return _strip_to_flat(parts.path) == _strip_to_flat(request.path)


def verify_proof(request, *, now: float | None = None) -> str:
    """Verify the request's DPoP proof and consume its jti. Returns the `jkt`.

    Raises ProofError with a reason; never returns a jkt for a proof that did
    not verify."""
    from canopy_sdk import jose
    from canopy_sdk.contract import ContractError, check_jti
    from canopy_sdk.keys import public_key_for

    proof = request.META.get(PROOF_HEADER, "")
    if not proof:
        raise ProofError("no DPoP proof")
    try:
        header = jose.unverified_header(proof, algorithms=(ALG,))
        if header.get("typ") != "dpop+jwt":
            raise ProofError("a DPoP proof must have typ dpop+jwt")
        jwk = header.get("jwk")
        key = public_key_for(jwk, ALG, algorithms=(ALG,))
        claims = jose.decode(proof, key, algorithms=[ALG], required=("iat", "jti", "htm", "htu"),
                             leeway=IAT_WINDOW_SECONDS)
        jti = check_jti(claims)
    except ContractError as exc:
        raise ProofError(str(exc)) from exc
    if str(claims.get("htm", "")).upper() != request.method.upper():
        raise ProofError("the proof is for a different HTTP method")
    if not htu_matches(str(claims.get("htu", "")), request):
        raise ProofError("the proof is for a different URL")
    iat = claims.get("iat")
    current = time.time() if now is None else now
    if not isinstance(iat, (int, float)) or isinstance(iat, bool) \
            or abs(current - iat) > IAT_WINDOW_SECONDS:
        raise ProofError("the proof is not fresh")
    jkt = thumbprint(jwk)
    # add() is atomic: False means the jti was already seen — a replay.
    if not cache.add(f"dpop-jti:{jkt}:{jti}", 1, timeout=JTI_TTL_SECONDS):
        raise ProofError("the proof's jti has already been used")
    return jkt


def runner_serves_agent(runner, agent) -> bool:
    """May this box read `agent`'s secrets? Four ways, and only four:

    - it RUNS the agent — `routing.runner_routes_agent`, the claim's own static
      gates (enabled routing, enrollment, the hold check), so a box the vault
      accepts is a box that could claim the agent's turns, and the reverse;
    - it HOLDS a live turn of the agent right now — a pinned dispatch or a chat
      bound to it claims there without routing, and that turn needs the secrets;
    - it is the agent's OWNER'S OWN laptop (not a cloud box), for working on the
      agent by hand;
    - it is a PRIVATE runner whose owner holds an active license for the agent
      (`private_runner_licensed`).

    A cloud box the owner also owns is not a reason on its own: it reads only what
    it runs (canopy-web#1563)."""
    from django.db.models import Q

    from .models import Runner, Turn
    from .routing import runner_routes_agent

    if runner.status == Runner.RETIRED:
        return False
    if runner_routes_agent(runner, agent):
        return True
    if (Turn.objects.filter(claimed_by=runner,
                            status__in=(Turn.CLAIMED, Turn.RUNNING, Turn.NEEDS_HUMAN))
            .filter(Q(agent=agent) | Q(chat_session__agent=agent)).exists()):
        return True
    if (runner.kind != Runner.CLOUD and bool(agent.owner_id)
            and runner.owner_id == agent.owner_id):
        return True
    return private_runner_licensed(runner, agent)


def private_runner_licensed(runner, agent) -> bool:
    """A PRIVATE runner whose owner holds an ACTIVE license for `agent` (canopy-web#1705):
    the person uses the agent on their own machine, and its secrets are read there only
    during their own turns (`licenses.credential_principal`)."""
    if not runner.private or runner.owner is None:
        return False
    from apps.agents import licenses

    return licenses.active(agent, runner.owner) is not None


def agents_served(runner):
    """The agents this box should PREPARE: the ones it runs
    (`routing.agents_routed_to`). Not the owner's-laptop or live-turn arms of
    `runner_serves_agent`: those read secrets for one session or one turn, and a
    box that staged more asked for vaults it would never use (fizzy, 2026-10-17)."""
    from .routing import agents_routed_to

    return agents_routed_to(runner)


def known_key(jkt: str):
    """The ACTIVE device key with this thumbprint, whichever agents it serves —
    so a refusal can tell an approved box that does not run the agent from a
    proof nobody approved."""
    from .models import RunnerDeviceKey

    return (RunnerDeviceKey.objects.select_related("runner")
            .filter(jkt=jkt, status=RunnerDeviceKey.ACTIVE).first())


def active_key_for_agent(agent, jkt: str):
    """The ACTIVE device key with this thumbprint, if its runner serves `agent`."""
    from .models import RunnerDeviceKey

    key = known_key(jkt)
    if key is None or not runner_serves_agent(key.runner, agent):
        return None
    RunnerDeviceKey.objects.filter(pk=key.pk).update(last_used_at=timezone.now())
    return key


def keys_for_agent(agent):
    """Every device key on a runner that serves this agent (for its Settings)."""
    from .models import Runner, RunnerDeviceKey

    runners = [r for r in Runner.objects.exclude(status=Runner.RETIRED)
               .filter(device_keys__isnull=False).distinct()
               if runner_serves_agent(r, agent)]
    return (RunnerDeviceKey.objects.filter(runner__in=runners)
            .select_related("runner", "approved_by").order_by("-created_at"))

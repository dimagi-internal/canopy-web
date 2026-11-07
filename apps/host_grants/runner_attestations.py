"""Runner attestations: a host site asks canopy whether a request really came from an
approved runner that serves an agent.

A runner already proves itself to canopy with a key a person approved (`harness.device_keys`,
`harness.aws_iam_proof`). A host site cannot check that proof: it holds no list of approved
keys. So the runner signs a proof for the SITE's endpoint, the site forwards it here with its
own signed request, and canopy answers who the key belongs to. The site then shows that
beside what the request says about itself, which is a claim and nothing more.

The proof is bound to the request it vouches for through its `htu`: the runner signs
`<site endpoint>/attest/<sha256 of the request's content>`, so a proof cannot be reused for a
different request, and its signature covers the digest with no change to either proof kind.
The site states the exact `htu` it expects and canopy requires the proof to carry precisely
that; it must sit on an origin the site registered, so a proof made for one site is of no use
to another. Each proof is single-use, as everywhere it is checked.
"""
from __future__ import annotations

import logging
from urllib.parse import urlsplit

from canopy_sdk import runner_attestations as contract
from canopy_sdk import sign_offs as wire
from django.conf import settings
from django.core.cache import cache

from apps.agents.models import Agent
from apps.embedding import assertions
from apps.harness import aws_iam_proof, device_keys
from apps.harness.models import Runner, RunnerDeviceKey

from . import client_identity, site_agents
from . import sign_offs as sign_off_service
from .site_agents import SiteAgentsRefused

log = logging.getLogger(__name__)
PATH = contract.PATH
KINDS = contract.KINDS
#: One answer for every refusal: a caller learns nothing about which runners or keys exist.
REFUSED = "no approved runner key proved this request"


def audiences() -> list[str]:
    bases = [b for b in (client_identity.public_base(),
                         (getattr(settings, "CANOPY_PUBLIC_BASE_URL", "") or "").rstrip("/")) if b]
    return list(dict.fromkeys(b + PATH for b in bases))


def verify_request(token: str):
    """`(app, claims)` for a request signed by a registered site's own key."""
    app = site_agents.site_for_request(token)
    try:
        keys = assertions.keys_for_app(app, token)
    except assertions.AssertionError_ as exc:
        raise SiteAgentsRefused(exc.code, exc.message) from exc

    def spend(claims: dict) -> None:
        ttl = max(1, int(claims["exp"]) - int(claims["iat"]) + 30)
        # Fails CLOSED: a cache that cannot record the jti reads as a replay.
        if not cache.add(f"runner-attest:jti:{app.pk}:{claims['jti']}", 1, timeout=ttl):
            raise wire.SignOffError("replayed", "this request has already been used")

    try:
        claims = wire.verify_request(token, keys, audiences=audiences(), asking=False, spend_jti=spend,
                                     algorithms=assertions.ALLOWED_ALGORITHMS)
    except wire.SignOffError as exc:
        raise SiteAgentsRefused(exc.code, exc.message, 400 if exc.code == "malformed" else 401) from exc
    if claims.get("iss") != app.name:
        raise SiteAgentsRefused("unknown_issuer", f"no host site named {claims.get('iss')!r}")
    return app, claims


def _htu_ok(app, expected: str):
    """The proof must be for EXACTLY the URL the site says it expects (the endpoint and a
    digest of the request), and that URL must sit on an origin this site registered."""
    origins = {o.lower() for o in sign_off_service.site_origins(app)}
    parts = urlsplit(expected or "")
    if parts.query or parts.fragment or parts.scheme != "https" or f"https://{parts.netloc.lower()}" not in origins:
        raise SiteAgentsRefused("malformed", "htu must be an https URL on an origin this site registered", 400)

    def ok(htu: str) -> bool:
        return htu == expected

    return ok


def attest(app, *, kind: str, proof: str, htu: str, method: str = "POST", agent: str = "") -> dict:
    """Who signed `proof`, as canopy knows them: `{runner, kind, jkt, agent, serves_agent}`.

    Raises `SiteAgentsRefused` (401, one wording) when no ACTIVE device key proved it; the
    reason goes to the log, not the caller."""
    ok = _htu_ok(app, htu)
    try:
        if kind == "aws-iam":
            key = aws_iam_proof.known_key(aws_iam_proof.verify_caller(proof, None, htu_ok=ok))
        elif kind == "dpop":
            key = device_keys.known_key(device_keys.verify_proof(None, proof=proof, method=method, htu_ok=ok))
        else:
            raise SiteAgentsRefused("malformed", f"kind must be one of {', '.join(KINDS)}", 400)
    except (aws_iam_proof.AwsProofError, device_keys.ProofError) as exc:
        log.info("runner attestation refused for %s: %s", app.name, exc)
        raise SiteAgentsRefused("proof_refused", REFUSED) from exc
    if key is None or key.status != RunnerDeviceKey.ACTIVE:
        log.info("runner attestation refused for %s: no ACTIVE device key matches", app.name)
        raise SiteAgentsRefused("proof_refused", REFUSED)
    runner = key.runner
    if runner.status == Runner.RETIRED:
        log.info("runner attestation refused for %s: %s is retired", app.name, runner.name)
        raise SiteAgentsRefused("proof_refused", REFUSED)
    named = Agent.objects.filter(slug=agent).first() if agent else None
    serves = bool(named is not None and device_keys.runner_serves_agent(runner, named))
    return {"runner": runner.name, "kind": key.kind, "jkt": key.jkt,
            "agent": named.slug if named else "", "serves_agent": serves}

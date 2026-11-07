"""`POST /api/sites/runner-attestations` — a host site asks who signed a runner's proof
(`runner_attestations.py`)."""
from __future__ import annotations

from ninja import Router, Schema
from ninja.errors import HttpError

from apps.api.route_gates import gate
from apps.embedding.audit import client_ip
from apps.embedding.rate_limit import (
    SiteAgentsRateLimitError,
    check_site_agents_client,
    check_site_agents_issuer,
)

from . import runner_attestations as service
from . import site_agents
from .api import _bearer

router = Router(tags=["host sites"])


class RunnerAttestationIn(Schema):
    #: `dpop` (a key the runner holds) or `aws-iam` (an EC2 runner's instance role).
    kind: str
    #: The runner's proof: the `DPoP` JWS, or the `X-Canopy-Aws-Proof` value.
    proof: str
    #: The exact URL the proof must be for: the site's endpoint and a digest of the request
    #: (`canopy_sdk.runner_attestations.bound_url`), on an origin the site registered.
    htu: str
    #: The agent the request says it speaks for, when it names one.
    agent: str = ""


class RunnerAttestationOut(Schema):
    runner: str
    kind: str
    jkt: str
    agent: str
    #: True only when the runner holding the key serves that agent.
    serves_agent: bool


@router.post("", auth=None, response=RunnerAttestationOut,
             summary="A host site asks who signed a runner's proof, signed")
@gate("host", kind="request")  # auth=None; a request signed by the site's own registered key IS the credential
def attest_runner(request, payload: RunnerAttestationIn):
    """Who holds the approved device key that signed `proof`.

    `Authorization: Bearer <JWT>` signed by the site's own key, as for
    `POST /api/sites/sign-offs` but with `aud` = this URL and no `sign_off_request`.
    The body carries the runner's proof and the exact `htu` it must be made for (an origin
    the site registered; the runner signs `<site endpoint>/attest/<sha256 of the request>`, so
    the proof vouches for that request only); single-use. Answers `{runner, kind, jkt, agent,
    serves_agent}`; 401 `proof_refused` when no ACTIVE approved device key proved it, with
    one wording whatever the reason.
    """
    try:
        check_site_agents_client(client_ip(request))
    except SiteAgentsRateLimitError as exc:
        raise HttpError(429, f"rate_limited: {exc}")
    token = _bearer(request)
    try:
        claimed = site_agents.site_for_request(token)
        check_site_agents_issuer(f"{claimed.workspace_id}:{claimed.name}")
        app, _claims = service.verify_request(token)
        return service.attest(app, kind=payload.kind, proof=payload.proof, htu=payload.htu,
                              agent=payload.agent)
    except SiteAgentsRateLimitError as exc:
        raise HttpError(429, f"rate_limited: {exc}")
    except site_agents.SiteAgentsRefused as exc:
        raise HttpError(exc.status, f"{exc.code}: {exc.message}")

"""`POST /api/runner-sessions/` — a runner's device key buys its API credential.

The box proves itself with an APPROVED `RunnerDeviceKey` and gets a
`RunnerAccessToken` back (an hour long, runner protocol only). No person's PAT
is involved, so nothing on the box outlives the hour and nothing a person
revokes in Settings → Tokens can strand a runner (2026-10-17, cloud-ec2-2).

Anonymous at the door (`auth=None`, and `/api/runner-sessions/` is public in
`apps/common/middleware.py`): the proof IS the authority, and it is checked
here exactly as `/credentials/resolve` checks it —

* `X-Canopy-Aws-Proof`: a signed-but-unsent `sts:GetCallerIdentity` canopy-web
  replays to the pinned STS endpoint (`aws_iam_proof.verify`), or
* `DPoP`: an RFC 9449 proof from the key the box holds (`device_keys.verify_proof`),

each bound to this URL, fresh within ±120 s and single-use. The key must be
ACTIVE — a person approved it behind a passkey — and belong to the runner named
in the body.
"""
from __future__ import annotations

import logging

from django.http import HttpRequest
from django.utils import timezone
from ninja import Router

from apps.api.errors import TYPE_AUTH, ProblemError
from apps.api.route_gates import gate

from . import aws_iam_proof, device_keys
from .models import Runner, RunnerAccessToken, RunnerDeviceKey
from .schemas import RunnerSessionIn, RunnerSessionOut

logger = logging.getLogger(__name__)

router = Router(auth=None, tags=["harness"])

_REFUSED = "no approved device key for this runner proved itself on this request"


def _refuse(why: str) -> ProblemError:
    # One answer for every refusal: a caller learns nothing about which runners
    # or keys exist. The reason goes to the log, where the operator reads it.
    logger.info("runner session refused: %s", why)
    return ProblemError(401, "Device proof refused", type_=TYPE_AUTH, detail=_REFUSED)


def _proven_key(request: HttpRequest) -> RunnerDeviceKey:
    aws = request.META.get(aws_iam_proof.PROOF_HEADER, "")
    try:
        if aws:
            key = aws_iam_proof.known_key(aws_iam_proof.verify(aws, request))
        elif request.META.get(device_keys.PROOF_HEADER):
            key = device_keys.known_key(device_keys.verify_proof(request))
        else:
            raise _refuse("no device proof")
    except (aws_iam_proof.AwsProofError, device_keys.ProofError) as exc:
        raise _refuse(str(exc)) from exc
    if key is None:
        raise _refuse("the proof verified but no ACTIVE device key matches it")
    return key


@router.post("/", response={201: RunnerSessionOut}, auth=None,
             summary="Exchange a runner's device-key proof for its API token")
# anonymous door; an ACTIVE key's proof is the authority
# a box trading its approved device key's proof for an hour-long runner token
@gate("device-proof", kind="ephemeral-credential", step_up="runner protocol")
def create_runner_session(request: HttpRequest, payload: RunnerSessionIn):
    """Prove an approved device key (`X-Canopy-Aws-Proof` or `DPoP`, bound to
    this URL) and get a runner token: `Authorization: Bearer` for the runner
    protocol only, valid for an hour."""
    key = _proven_key(request)
    runner = key.runner
    if runner.pk != payload.runner:
        raise _refuse(f"key {key.pk} belongs to {runner.name}, not {payload.runner}")
    if runner.status == Runner.RETIRED:
        raise _refuse(f"{runner.name} is retired")
    owner = runner.owner
    if owner is None or not owner.is_active or hasattr(owner, "system_account"):
        raise _refuse(f"{runner.name} has no active person as its owner")
    raw, token = RunnerAccessToken.mint(key)
    RunnerDeviceKey.objects.filter(pk=key.pk).update(last_used_at=timezone.now())
    return 201, RunnerSessionOut(access_token=raw, expires_at=token.expires_at, runner=runner.pk)

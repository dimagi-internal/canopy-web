"""Which machine is asking: the device proof every material, lease and write-back
route requires (contract §0.6).

A proof is an RFC 9449 DPoP proof signed by a runner device key
(`apps.harness.device_keys.verify_proof`), or an EC2 box's instance-role proof
(`apps.harness.aws_iam_proof`), as a credential resolve accepts. The key it names
must be ACTIVE with an APPROVED ECDH key: that ECDH key is what the envelopes are
sealed to and what the writer MACs are keyed by.

- `proving_device(request, runner)`: a runner route. The key must be on `runner`,
  the box whose runner token made the request; anything else is 401
  `device_proof_required`.
- `proving_machine(request, user)`: a person's own CLI. A missing or bad proof is
  401 `device_proof_required`; a good proof by a key that is not an enrolled
  machine of `user`'s (any kind, a `computer` too) is 403 `machine_not_enrolled`.
"""
from __future__ import annotations

import logging

from django.http import HttpRequest

from apps.harness import aws_iam_proof, device_keys
from apps.harness.models import Runner, RunnerDeviceKey

from .wire import WireError

logger = logging.getLogger(__name__)

ENROLL = "Enroll this computer: canopy vault device --enroll"


def _proof_required(why: str) -> WireError:
    return WireError(401, "device_proof_required", f"A device proof from this machine is required ({why}).",
                     reason=why)


def _proved_key(request: HttpRequest) -> RunnerDeviceKey | None:
    """The ACTIVE key the request's proof verifies for (None: a good proof by a key
    canopy-web does not know). A missing or bad proof is a 401."""
    aws = request.META.get(aws_iam_proof.PROOF_HEADER, "")
    if aws:
        try:
            role = aws_iam_proof.verify_caller(aws, request)
        except aws_iam_proof.AwsProofError as exc:
            logger.info("connections: aws iam proof refused: %s", exc)
            raise _proof_required(f"AWS IAM proof refused: {exc}") from None
        return aws_iam_proof.known_key(role)
    if not request.META.get(device_keys.PROOF_HEADER):
        raise _proof_required("no device proof")
    try:
        jkt = device_keys.verify_proof(request)
    except device_keys.ProofError as exc:
        logger.info("connections: device proof refused: %s", exc)
        raise _proof_required(f"DPoP proof refused: {exc}") from None
    return device_keys.known_key(jkt)


def _ecdh_ready(key: RunnerDeviceKey | None) -> bool:
    return (key is not None and key.status == RunnerDeviceKey.ACTIVE and bool(key.ecdh_jkt)
            and key.ecdh_approved_at is not None and key.runner.status != Runner.RETIRED)


def proving_device(request: HttpRequest, runner: Runner) -> RunnerDeviceKey:
    """The device key on `runner` that proved this request; 401 otherwise. A proof by
    another box's key does not go with this box's runner token."""
    key = _proved_key(request)
    if not _ecdh_ready(key) or key.runner_id != runner.pk:
        raise _proof_required("the proof is not by an approved device key of this runner")
    return key


def proving_machine(request: HttpRequest, user) -> RunnerDeviceKey:
    """The enrolled machine of `user`'s that proved this request: a key on a runner
    they own, any kind. 401 without a good proof; 403 `machine_not_enrolled` for a
    key that is not one of their enrolled machines."""
    key = _proved_key(request)
    if not _ecdh_ready(key) or key.runner.owner_id != user.pk:
        raise WireError(403, "machine_not_enrolled",
                        f"This computer is not one of your enrolled machines. {ENROLL}")
    return key

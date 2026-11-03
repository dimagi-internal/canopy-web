"""A cloud runner reading its own Claude credentials out of its vault (spec
2026-10-23-runner-claude-credentials-in-the-vault-design.md).

`GET /api/harness/runners/{id}/credential/resolve` answers with CIPHERTEXT only the
box's own device key opens: each vault serving the runner
(`workspace_vaults.runner_vaults`), this device's copy of the vault key, and the values
the runner's credential list names. canopy-web cannot read them; the box opens them
with `canopy vault runner-credentials`, the code that opens an agent's vault layers.

Who may ask is narrower than the old plaintext bundle (`GET …/credential`, anything
signed in as the runner's owner): the box's OWN runner token, with a device proof of an
active key on that same runner. A person's PAT or browser session never gets it, and
every value handed out is logged (`RunnerCredentialRead`) and recorded as a fleet event.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass

from django.http import HttpRequest
from django.utils import timezone

from apps.agents.credentials import workspace_vaults
from apps.events import services as events

from . import aws_iam_proof, device_keys
from . import runners as harness_runners
from .models import RunnerCredentialRead, RunnerDeviceKey

logger = logging.getLogger(__name__)

RUNNER_ONLY = "runner_only"
DEVICE_PROOF_REQUIRED = "device_proof_required"


@dataclass(frozen=True)
class Refusal:
    status: int
    code: str
    detail: str


def device_key_for(request: HttpRequest, runner):
    """`(key, why)`: the ACTIVE device key on `runner` whose proof this request
    carries — an EC2 box's instance role (`X-Canopy-Aws-Proof`) or a DPoP proof —
    else None and why. A key on any other runner is no key at all here."""
    aws = request.META.get(aws_iam_proof.PROOF_HEADER, "")
    if aws:
        try:
            key = aws_iam_proof.known_key(aws_iam_proof.verify_caller(aws, request))
        except aws_iam_proof.AwsProofError as exc:
            return None, f"AWS IAM proof refused: {exc}"
    elif request.META.get("HTTP_DPOP"):
        try:
            key = device_keys.known_key(device_keys.verify_proof(request))
        except device_keys.ProofError as exc:
            return None, f"DPoP proof refused: {exc}"
    else:
        return None, "no device proof"
    if key is None:
        return None, "the proof is not from an approved device key"
    if key.runner_id != runner.pk:
        return None, f"the device proof is {key.runner.name}'s, not {runner.name}'s"
    RunnerDeviceKey.objects.filter(pk=key.pk).update(last_used_at=timezone.now())
    return key, ""


def wanted_names(runner) -> set[str]:
    """The secret names the runner's credential list uses: one per token or API key."""
    return {e.vault_secret_name for e in harness_runners._entries(runner) if e.vault_secret_name}


def _box_key(request: HttpRequest, runner):
    """`(device_key, None)` when this request is the box itself — its own runner token
    and a proof of an active device key on it — else `(None, Refusal)`."""
    rtok = getattr(request, "runner_token", None)
    if rtok is None or rtok.runner_id != runner.pk:
        return None, Refusal(403, RUNNER_ONLY, f"only {runner.name}'s own runner token reaches its "
                                               "credentials; a person's token or browser session never does")
    key, why = device_key_for(request, runner)
    if key is None:
        return None, Refusal(401, DEVICE_PROOF_REQUIRED, why)
    return key, None


def box_refusal(request: HttpRequest, runner) -> Refusal | None:
    """Why this request is not the box itself (`_box_key`), or None."""
    return _box_key(request, runner)[1]


def resolve(request: HttpRequest, runner) -> list[dict] | Refusal:
    key, refused = _box_key(request, runner)
    if refused is not None:
        return refused
    layers = workspace_vaults.runner_layers_payload(runner, key, wanted_names(runner))
    _record_read(runner, key, layers)
    return layers


def _record_read(runner, key, layers: list[dict]) -> None:
    """The read log and the fleet event. Neither may deny a box its credentials."""
    handed = [(layer["vault_id"], name) for layer in layers for name in layer["values"]]
    try:
        RunnerCredentialRead.objects.bulk_create(
            [RunnerCredentialRead(runner=runner, device_key=key, vault_id=vault_id, name=name)
             for vault_id, name in handed])
    except Exception:  # noqa: BLE001 - a log hiccup must not deny a box its credentials
        logger.exception("runner credential read log failed for %s", runner.pk)
    try:
        events.record(
            [{
                "source": "harness.runner_credentials",
                "kind": "runner.credentials.resolved",
                "level": "info",
                "key": str(runner.pk),
                "summary": f"{len(handed)} credential(s) read from the vault by {runner.name}",
                "payload": {"runner": str(runner.pk), "device_key": str(key.pk),
                            "names": sorted(name for _, name in handed),
                            "vaults": sorted({vault_id for vault_id, _ in handed})},
            }],
            workspace=runner.workspace,
        )
    except Exception:  # noqa: BLE001 - an audit hiccup must not deny a box its credentials
        pass

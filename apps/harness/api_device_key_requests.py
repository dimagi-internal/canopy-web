"""`POST /api/runner-device-key-requests/` — a runner whose sign-in token was revoked
asks its owner for a device key.

A box enrolls its device key with its owner's PAT (`/api/runner-device-keys/`), and
from then on signs in with the key (`/api/runner-sessions/`). A box that never got as
far as a key and whose PAT is then revoked is stranded: it cannot heartbeat, cannot
enroll, and so never puts an approve link in front of anyone. On 2026-10-17 a token
cleanup revoked every PAT the haldimagi and jj laptop runners had, before either had
a key.

This door lets such a box ask, and only ask: it creates a PENDING key — which nothing
accepts until a person approves it in a browser behind a passkey — and tells the
owner. The box proves two things:

* it holds a token its runner's owner minted (`owner_token`, in the body: the bearer
  middleware would refuse a revoked one before any view). Revoked is allowed — that is
  the whole case — but only recently (`STALE_TOKEN_DAYS`), never another person's, and
  never a deactivated owner's;
* it holds the key it is registering: a DPoP proof for THIS URL signed by that key, or
  for an EC2 box an instance-role proof in `proof`, as `/api/runner-device-keys/` does.

It changes nothing about a key that already exists (a revoked key stays revoked, an
active one stays active) and keeps at most `MAX_PENDING` pending keys per runner.
"""
from __future__ import annotations

import hashlib
import logging
from datetime import timedelta

from django.db.models import Q
from django.http import HttpRequest
from django.utils import timezone
from ninja import Router

from apps.api.errors import TYPE_AUTH, ProblemError

from . import device_keys
from .models import Runner, RunnerDeviceKey
from .schemas import RunnerDeviceKeyCreatedOut, RunnerDeviceKeyIn

logger = logging.getLogger(__name__)

router = Router(auth=None, tags=["harness"])

STALE_TOKEN_DAYS = 30
MAX_PENDING = 3
_REFUSED = "this request does not prove a token of the runner's owner and the key it names"


class RunnerDeviceKeyRequestIn(RunnerDeviceKeyIn):
    owner_token: str


def _refuse(why: str) -> ProblemError:
    # One answer for every refusal: nothing about which runners, owners or keys exist.
    logger.info("device key request refused: %s", why)
    return ProblemError(401, "Device key request refused", type_=TYPE_AUTH, detail=_REFUSED)


def _owner_token_ok(raw: str, runner: Runner) -> bool:
    from apps.tokens.models import PersonalToken

    if not raw or runner.owner_id is None:
        return False
    since = timezone.now() - timedelta(days=STALE_TOKEN_DAYS)
    return (PersonalToken.objects
            .filter(token_hash=hashlib.sha256(raw.encode()).hexdigest(), user_id=runner.owner_id,
                    user__is_active=True, user__system_account__isnull=True)
            .filter(Q(revoked_at__isnull=True) | Q(revoked_at__gte=since))
            .exists())


def _holds_key(request: HttpRequest, payload: RunnerDeviceKeyIn) -> tuple[dict, str, str]:
    """`(public_jwk, role_arn, jkt)` of the key the box proved it holds."""
    if payload.kind == RunnerDeviceKey.AWS_IAM:
        from .api_device_keys import _verified_role

        role_arn, jkt = _verified_role(request, payload)
        return {}, role_arn, jkt
    try:
        jwk = device_keys.normalize_public_jwk(payload.public_jwk)
        proven = device_keys.verify_proof(request)
    except (device_keys.KeyError_, device_keys.ProofError) as exc:
        raise _refuse(f"key/proof: {exc}") from exc
    jkt = device_keys.thumbprint(jwk)
    if proven != jkt:
        raise _refuse("the proof was signed by a different key than the one registered")
    return jwk, "", jkt


@router.post("/", response={200: RunnerDeviceKeyCreatedOut, 201: RunnerDeviceKeyCreatedOut},
             auth=None, summary="A runner with a revoked token asks for a (pending) device key")
def request_runner_device_key(request: HttpRequest, payload: RunnerDeviceKeyRequestIn):
    """Prove a token of the runner's owner (revoked within 30 days is fine) and the
    key being registered; get a PENDING key and its `approve_url`. The owner is
    emailed and pushed the link."""
    from .api_device_keys import _created, _ecdh, _set_ecdh

    runner = (Runner.objects.exclude(status=Runner.RETIRED).select_related("owner")
              .filter(pk=payload.runner).first())
    if runner is None or not _owner_token_ok(payload.owner_token, runner):
        raise _refuse("no such runner, or the token is not its owner's")
    jwk, role_arn, jkt = _holds_key(request, payload)
    ecdh_jwk, ecdh_jkt = _ecdh(payload)

    existing = RunnerDeviceKey.objects.filter(jkt=jkt).first()
    if existing is not None:
        if existing.runner_id != runner.pk:
            raise _refuse("the key belongs to another runner")
        if existing.status == RunnerDeviceKey.PENDING:
            _set_ecdh(existing, ecdh_jwk, ecdh_jkt)
        return 200, _created(existing)
    if RunnerDeviceKey.objects.filter(runner=runner, status=RunnerDeviceKey.PENDING).count() >= MAX_PENDING:
        raise ProblemError(429, "Too many pending keys",
                           detail=f"{runner.name} already has {MAX_PENDING} keys waiting for approval")
    key = RunnerDeviceKey.objects.create(
        runner=runner, public_jwk=jwk, role_arn=role_arn, jkt=jkt, kind=payload.kind,
        label=payload.label.strip(), created_by=runner.owner,
        ecdh_public_jwk=ecdh_jwk or {}, ecdh_jkt=ecdh_jkt,
    )
    from . import device_key_notify

    device_key_notify.notify_pending_on_commit(key)  # owner + runner admins get the approve link
    return 201, _created(key)

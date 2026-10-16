"""Runner device keys — `/api/runner-device-keys/` (Canopy Vault, canopy-web#1538).

The contract, which canopy's CLI/runner half builds against (canopy#946):

* `POST /api/runner-device-keys/` `{runner, public_jwk, kind, label}` →
  `{id, jkt, status: "pending", approve_url}`; the same `jkt` again is idempotent.
  An EC2 box registers its instance role instead (canopy-web#1549):
  `{runner, kind: "aws-iam", role_arn, proof}`, the proof an
  `X-Canopy-Aws-Proof` value for this URL (`aws_iam_proof.py`).
* `GET /api/runner-device-keys/?runner=<id>` → the keys, with status.
* `POST /api/runner-device-keys/{id}/approve` → `active`. A BROWSER session of
  the runner's owner or an admin, behind a fresh passkey step-up.
* `DELETE /api/runner-device-keys/{id}` → `revoked`, immediately.

Who: the runner's owner or an explicit runner admin (`can_administer_runner`)
for every route. A key belongs to a box, and the box is administered by them.
"""
from __future__ import annotations

import uuid

from django.conf import settings
from django.db import IntegrityError, transaction
from django.db.models import Q
from django.http import HttpRequest
from django.utils import timezone
from ninja import Router
from ninja.errors import HttpError

from apps.api.auth import session_auth
from apps.events import audit
from apps.passkeys import stepup

from . import device_keys
from .models import Runner, RunnerAdmin, RunnerDeviceKey
from .runners import can_administer_runner
from .schemas import RunnerDeviceKeyCreatedOut, RunnerDeviceKeyIn, RunnerDeviceKeyOut

router = Router(auth=session_auth, tags=["harness"])


def _administered_runner_or_404(request: HttpRequest, runner_id) -> Runner:
    runner = Runner.objects.exclude(status=Runner.RETIRED).filter(pk=runner_id).first()
    # 404, not 403, like the rest of the harness: do not confirm a runner exists.
    if runner is None or not can_administer_runner(request.user, runner):
        raise HttpError(404, "runner not found")
    return runner


def _key_or_404(request: HttpRequest, key_id: uuid.UUID) -> RunnerDeviceKey:
    key = RunnerDeviceKey.objects.select_related("runner", "approved_by").filter(pk=key_id).first()
    if key is None or not can_administer_runner(request.user, key.runner):
        raise HttpError(404, "device key not found")
    return key


def approve_url(key: RunnerDeviceKey) -> str:
    base = (getattr(settings, "CANOPY_PUBLIC_BASE_URL", "") or "").rstrip("/")
    return f"{base}/settings?device_key={key.pk}#device-keys"


def key_out(key: RunnerDeviceKey, *, can_manage: bool) -> RunnerDeviceKeyOut:
    return RunnerDeviceKeyOut(
        id=key.pk, runner=key.runner_id, runner_name=key.runner.name, jkt=key.jkt,
        kind=key.kind, label=key.label, role_arn=key.role_arn, status=key.status,
        ecdh_jkt=key.ecdh_jkt, ecdh_public_jwk=key.ecdh_public_jwk or {},
        created_at=key.created_at,
        approved_at=key.approved_at,
        approved_by_email=(key.approved_by.email if key.approved_by_id else ""),
        revoked_at=key.revoked_at, last_used_at=key.last_used_at, can_manage=can_manage,
    )


def _is_browser_session(request: HttpRequest) -> bool:
    """Signed in by canopy's own session cookie — not a PAT, OAuth or MCP
    token. The token middleware stamps `auth_method` on every token login;
    its absence means the session did it."""
    return (getattr(request, "auth_method", None) is None
            and not request.META.get("HTTP_AUTHORIZATION", ""))


@router.post("/", response={200: RunnerDeviceKeyCreatedOut, 201: RunnerDeviceKeyCreatedOut},
             summary="Register a runner device key (pending until approved)")
def register_runner_device_key(request: HttpRequest, payload: RunnerDeviceKeyIn):
    """The box registers the public half of a key it holds. It starts `pending`;
    nothing it signs is accepted until a person approves it at `approve_url`.
    Re-posting the same key returns the existing row (200) whatever its status —
    a revoked key stays revoked."""
    runner = _administered_runner_or_404(request, payload.runner)
    role_arn = ""
    if payload.kind == RunnerDeviceKey.AWS_IAM:
        role_arn, jkt = _verified_role(request, payload)
        jwk: dict = {}
    else:
        try:
            jwk = device_keys.normalize_public_jwk(payload.public_jwk)
        except device_keys.KeyError_ as exc:
            raise HttpError(422, str(exc)) from exc
        jkt = device_keys.thumbprint(jwk)
    ecdh_jwk, ecdh_jkt = _ecdh(payload)

    existing = RunnerDeviceKey.objects.filter(jkt=jkt).first()
    if existing is not None:
        if existing.runner_id != runner.pk:
            raise HttpError(409, "this key is already registered to a different runner")
        _set_ecdh(existing, ecdh_jwk, ecdh_jkt)
        return 200, _created(existing)
    try:
        with transaction.atomic():
            key = RunnerDeviceKey.objects.create(
                runner=runner, public_jwk=jwk, role_arn=role_arn, jkt=jkt, kind=payload.kind,
                label=payload.label.strip(), created_by=request.user,
                ecdh_public_jwk=ecdh_jwk or {}, ecdh_jkt=ecdh_jkt,
            )
    except IntegrityError:  # a concurrent identical POST won the race
        key = RunnerDeviceKey.objects.get(jkt=jkt)
        _set_ecdh(key, ecdh_jwk, ecdh_jkt)
        return 200, _created(key)
    return 201, _created(key)


def _created(key: RunnerDeviceKey) -> RunnerDeviceKeyCreatedOut:
    return RunnerDeviceKeyCreatedOut(id=key.pk, jkt=key.jkt, status=key.status,
                                     approve_url=approve_url(key), ecdh_jkt=key.ecdh_jkt)


def _ecdh(payload: RunnerDeviceKeyIn) -> tuple[dict | None, str]:
    """The registration's ECDH public key, checked, and its thumbprint."""
    if not payload.ecdh_public_jwk:
        return None, ""
    try:
        jwk = device_keys.normalize_public_jwk(payload.ecdh_public_jwk)
    except device_keys.KeyError_ as exc:
        raise HttpError(422, f"ecdh_public_jwk: {exc}") from exc
    return jwk, device_keys.thumbprint(jwk)


def _set_ecdh(key: RunnerDeviceKey, jwk: dict | None, jkt: str) -> None:
    """Attach or change a known key's ECDH key (canopy-web#1550). An ACTIVE key
    goes back to `pending`: which key receives an agent's vault key is a trust
    decision a person makes, not one the box makes by re-posting. Wraps to the
    old ECDH key are dropped (they name its thumbprint). Re-sending the same
    key, or none, changes nothing; a revoked key stays revoked."""
    if jwk is None or jkt == key.ecdh_jkt or key.status == RunnerDeviceKey.REVOKED:
        return
    from apps.agents import zk

    before = key.status
    with transaction.atomic():
        key.ecdh_public_jwk, key.ecdh_jkt = jwk, jkt
        fields = ["ecdh_public_jwk", "ecdh_jkt"]
        if key.status == RunnerDeviceKey.ACTIVE:
            key.status, key.approved_by, key.approved_at = RunnerDeviceKey.PENDING, None, None
            fields += ["status", "approved_by", "approved_at"]
        key.save(update_fields=fields)
        zk.forget_device_wraps(key)
    if before != key.status:
        audit.note_change(scope=f"runner-device-key:{key.runner.name}/{key.jkt[:12]}",
                          before=before, after=key.status)


def _verified_role(request: HttpRequest, payload: RunnerDeviceKeyIn) -> tuple[str, str]:
    """`(canonical role ARN, jkt)` for an aws-iam registration, after its proof
    — signed for THIS URL — came back from STS as `role_arn` (canopy-web#1549).
    Registering a role you cannot sign as is refused, so a pending row always
    names an identity the registering box actually holds."""
    from . import aws_iam_proof

    if payload.public_jwk:
        raise HttpError(422, "an aws-iam key has no public_jwk; send role_arn and proof")
    try:
        claimed = aws_iam_proof.canonical_role_arn(payload.role_arn)
    except aws_iam_proof.AwsProofError as exc:
        raise HttpError(422, f"role_arn: {exc}") from exc
    if not payload.proof:
        raise HttpError(422, "an aws-iam key needs a proof (a signed sts:GetCallerIdentity)")
    try:
        proven = aws_iam_proof.verify(payload.proof, request)
    except aws_iam_proof.AwsProofError as exc:
        raise HttpError(422, f"proof: {exc}") from exc
    if proven != claimed:
        raise HttpError(422, f"the proof was signed by {proven}, not {claimed}")
    return claimed, aws_iam_proof.role_jkt(claimed)


@router.get("/", response=list[RunnerDeviceKeyOut], summary="List runner device keys")
def list_runner_device_keys(request: HttpRequest, runner: uuid.UUID | None = None):
    """Keys on one runner (`?runner=`), or on every runner the caller owns or
    administers."""
    if runner is not None:
        r = _administered_runner_or_404(request, runner)
        qs = RunnerDeviceKey.objects.filter(runner=r)
    else:
        admin_of = RunnerAdmin.objects.filter(user=request.user).values("runner_id")
        qs = RunnerDeviceKey.objects.filter(
            Q(runner__owner=request.user) | Q(runner_id__in=admin_of),
        ).exclude(runner__status=Runner.RETIRED)
    qs = qs.select_related("runner", "approved_by").order_by("-created_at")
    return [key_out(k, can_manage=True) for k in qs]


@router.post("/{key_id}/approve", response=RunnerDeviceKeyOut,
             summary="Approve a pending device key (browser session + passkey step-up)")
def approve_runner_device_key(request: HttpRequest, key_id: uuid.UUID):
    """`pending` → `active`. A revoked key cannot be revived (409); approving an
    active key is a no-op."""
    if not _is_browser_session(request):
        raise HttpError(403, "approving a device key needs a browser session, not a token")
    key = _key_or_404(request, key_id)
    # A fresh passkey confirmation (canopy-web#1538): an active key unlocks an
    # agent's secrets for that box, so a stolen session cookie must not suffice.
    # 403 `step_up_required` → the SPA prompts for the passkey and retries.
    stepup.require_recent_step_up(request)
    if key.status == RunnerDeviceKey.REVOKED:
        raise HttpError(409, "this key was revoked; register a new one")
    before = key.status
    if key.status == RunnerDeviceKey.PENDING:
        key.status = RunnerDeviceKey.ACTIVE
        key.approved_by = request.user
        key.approved_at = timezone.now()
        key.save(update_fields=["status", "approved_by", "approved_at"])
    audit.note_change(scope=f"runner-device-key:{key.runner.name}/{key.jkt[:12]}",
                      before=before, after=key.status)
    out = key_out(key, can_manage=True)
    out.zk_pending_agents = _zk_agents_waiting_on(request.user, key)
    return out


def _zk_agents_waiting_on(user, key: RunnerDeviceKey) -> list[str]:
    """Zero-knowledge agents `user` administers that this (now active) key
    should be granted the vault key of — the "Grant" offer after approving."""
    if key.status != RunnerDeviceKey.ACTIVE or not key.ecdh_jkt:
        return []
    from apps.agents.models import Agent, AgentVaultWrap

    out = []
    for agent in Agent.objects.filter(zk_enabled=True).select_related("workspace", "owner"):
        if (agent.is_admin(user) and device_keys.runner_serves_agent(key.runner, agent)
                and not AgentVaultWrap.objects.filter(agent=agent, kv=agent.zk_kv, kind="device",
                                                      device_key=key).exists()):
            out.append(agent.slug)
    return sorted(out)


@router.delete("/{key_id}", response=RunnerDeviceKeyOut, summary="Revoke a device key")
def revoke_runner_device_key(request: HttpRequest, key_id: uuid.UUID):
    """Immediate: the next proof signed by this key is refused. Idempotent."""
    key = _key_or_404(request, key_id)
    before = key.status
    if key.status != RunnerDeviceKey.REVOKED:
        key.status = RunnerDeviceKey.REVOKED
        key.revoked_by = request.user
        key.revoked_at = timezone.now()
        key.save(update_fields=["status", "revoked_by", "revoked_at"])
    audit.note_change(scope=f"runner-device-key:{key.runner.name}/{key.jkt[:12]}",
                      before=before, after=key.status)
    return key_out(key, can_manage=True)

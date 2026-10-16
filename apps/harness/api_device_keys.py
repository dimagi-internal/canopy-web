"""Runner device keys — `/api/runner-device-keys/` (Canopy Vault, canopy-web#1538).

The contract, which canopy's CLI/runner half builds against (canopy#946):

* `POST /api/runner-device-keys/` `{runner, public_jwk, kind, label}` →
  `{id, jkt, status: "pending", approve_url}`; the same `jkt` again is idempotent.
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
        kind=key.kind, label=key.label, status=key.status, created_at=key.created_at,
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
    try:
        jwk = device_keys.normalize_public_jwk(payload.public_jwk)
    except device_keys.KeyError_ as exc:
        raise HttpError(422, str(exc)) from exc
    jkt = device_keys.thumbprint(jwk)

    existing = RunnerDeviceKey.objects.filter(jkt=jkt).first()
    if existing is not None:
        if existing.runner_id != runner.pk:
            raise HttpError(409, "this key is already registered to a different runner")
        return 200, RunnerDeviceKeyCreatedOut(
            id=existing.pk, jkt=jkt, status=existing.status, approve_url=approve_url(existing))
    try:
        with transaction.atomic():
            key = RunnerDeviceKey.objects.create(
                runner=runner, public_jwk=jwk, jkt=jkt, kind=payload.kind,
                label=payload.label.strip(), created_by=request.user,
            )
    except IntegrityError:  # a concurrent identical POST won the race
        key = RunnerDeviceKey.objects.get(jkt=jkt)
        return 200, RunnerDeviceKeyCreatedOut(
            id=key.pk, jkt=jkt, status=key.status, approve_url=approve_url(key))
    return 201, RunnerDeviceKeyCreatedOut(
        id=key.pk, jkt=jkt, status=key.status, approve_url=approve_url(key))


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
    # STEP-UP: apps.passkeys wires require_recent_step_up here (canopy-web#1538)
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
    return key_out(key, can_manage=True)


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

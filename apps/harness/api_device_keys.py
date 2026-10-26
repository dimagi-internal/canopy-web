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
for every route. A key belongs to a box, and the box is administered by them —
except that on a PRIVATE runner only its owner registers or approves a key
(`may_trust_device_keys`): that box's token signs in as its owner.
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

from apps.agents.models import Agent
from apps.api.auth import session_auth
from apps.api.route_gates import gate
from apps.events import audit
from apps.passkeys import stepup

from . import device_key_notify, device_keys
from .models import Runner, RunnerAdmin, RunnerDeviceKey, RunnerFleet
from .runners import can_administer_runner, may_trust_device_keys, set_runner_machine
from .schemas import DeviceKeyAgentOut, RunnerDeviceKeyCreatedOut, RunnerDeviceKeyIn, RunnerDeviceKeyOut

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


def _may_trust_or_403(request: HttpRequest, runner: Runner) -> None:
    if not may_trust_device_keys(request.user, runner):
        raise HttpError(403, f"{runner.name} is its owner's private runner; only its owner "
                             "may register or approve a device key on it")


def approve_path(key: RunnerDeviceKey) -> str:
    """Settings → Your runners with this key highlighted, app-relative."""
    return f"/settings?device_key={key.pk}#your-runners"


def approve_url(key: RunnerDeviceKey) -> str:
    base = (getattr(settings, "CANOPY_PUBLIC_BASE_URL", "") or "").rstrip("/")
    return f"{base}{approve_path(key)}"


def waiting_approval_url(runner) -> str:
    """`approve_url` of this runner's newest key that waits for a person — pending,
    or active with a new ECDH key not yet approved — or "" when none does."""
    keys = RunnerDeviceKey.objects.filter(runner=runner).exclude(status=RunnerDeviceKey.REVOKED)
    for key in keys.order_by("-created_at"):
        if key.status == RunnerDeviceKey.PENDING or key.ecdh_waiting:
            return approve_url(key)
    return ""


def key_out(key: RunnerDeviceKey, *, can_manage: bool) -> RunnerDeviceKeyOut:
    return RunnerDeviceKeyOut(
        id=key.pk, runner=key.runner_id, runner_name=key.runner.name,
        runner_host=key.runner.host, runner_kind=key.runner.kind, jkt=key.jkt,
        kind=key.kind, label=key.label, role_arn=key.role_arn, status=key.status,
        ecdh_jkt=key.ecdh_jkt, ecdh_public_jwk=key.ecdh_public_jwk or {}, ecdh_store=key.ecdh_store,
        reapproval=key.status == RunnerDeviceKey.PENDING and key.approved_at is not None,
        ecdh_waiting=key.ecdh_waiting,
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
# lands pending; on a private runner, owner only
# a box registering its own key: lands `pending`, trusted only once a person
# approves it (approve_runner_device_key, a setting)
@gate("runner-admin", kind="runner-protocol")
def register_runner_device_key(request: HttpRequest, payload: RunnerDeviceKeyIn):
    """The box registers the public half of a key it holds. It starts `pending`;
    nothing it signs is accepted until a person approves it at `approve_url`.
    Re-posting the same key returns the existing row (200) whatever its status —
    a revoked key stays revoked."""
    runner = _administered_runner_or_404(request, payload.runner)
    _may_trust_or_403(request, runner)
    role_arn = ""
    if payload.kind == RunnerDeviceKey.AWS_IAM:
        role_arn, jkt = verified_role(request, payload)
        jwk: dict = {}
    else:
        try:
            jwk = device_keys.normalize_public_jwk(payload.public_jwk)
        except device_keys.KeyError_ as exc:
            raise HttpError(422, str(exc)) from exc
        jkt = device_keys.thumbprint(jwk)
    ecdh_jwk, ecdh_jkt = checked_ecdh(payload)
    store = payload.ecdh_store if ecdh_jwk is not None and payload.ecdh_store in ECDH_STORES else ""

    existing = RunnerDeviceKey.objects.filter(jkt=jkt).first()
    if existing is not None:
        if existing.runner_id != runner.pk:
            raise HttpError(409, "this key is already registered to a different runner")
        set_ecdh_and_store(existing, ecdh_jwk, ecdh_jkt, store)
        return 200, created_out(existing)
    try:
        with transaction.atomic():
            key = RunnerDeviceKey.objects.create(
                runner=runner, public_jwk=jwk, role_arn=role_arn, jkt=jkt, kind=payload.kind,
                label=payload.label.strip(), created_by=request.user,
                ecdh_public_jwk=ecdh_jwk or {}, ecdh_jkt=ecdh_jkt, ecdh_store=store,
            )
    except IntegrityError:  # a concurrent identical POST won the race
        key = RunnerDeviceKey.objects.get(jkt=jkt)
        set_ecdh_and_store(key, ecdh_jwk, ecdh_jkt, store)
        return 200, created_out(key)
    device_key_notify.notify_pending_on_commit(key)
    return 201, created_out(key)


#: Where a box may say its ECDH private key lives (`RunnerDeviceKey.ecdh_store`).
#: `kms-broker`: an isolation unit's KMS key, used through its box's root-only broker.
ECDH_STORES = frozenset({"secure-enclave", "tpm", "aws-kms", "software", "kms-broker"})


def set_ecdh_and_store(key: RunnerDeviceKey, jwk: dict | None, jkt: str, store: str) -> None:
    """`set_ecdh`, then record where the box says the key's CURRENT ECDH key
    lives. Display only, so it changes no status and needs no approval. A new
    ECDH key without a word about it clears the old label rather than inherit
    it; a claim is only ever taken alongside the ECDH key it describes."""
    before = key.ecdh_jkt
    set_ecdh(key, jwk, jkt)
    if key.ecdh_jkt != before:
        new = store
    elif store and jkt and jkt == key.ecdh_jkt:
        new = store
    else:
        return
    if new != key.ecdh_store:
        key.ecdh_store = new
        key.save(update_fields=["ecdh_store"])


def created_out(key: RunnerDeviceKey) -> RunnerDeviceKeyCreatedOut:
    return RunnerDeviceKeyCreatedOut(id=key.pk, jkt=key.jkt, status=key.status,
                                     approve_url=approve_url(key), ecdh_jkt=key.ecdh_jkt)


def checked_ecdh(payload: RunnerDeviceKeyIn) -> tuple[dict | None, str]:
    """The registration's ECDH public key, checked, and its thumbprint."""
    if not payload.ecdh_public_jwk:
        return None, ""
    try:
        jwk = device_keys.normalize_public_jwk(payload.ecdh_public_jwk)
    except device_keys.KeyError_ as exc:
        raise HttpError(422, f"ecdh_public_jwk: {exc}") from exc
    return jwk, device_keys.thumbprint(jwk)


def set_ecdh(key: RunnerDeviceKey, jwk: dict | None, jkt: str) -> None:
    """Attach or change a known key's ECDH key (canopy-web#1550). The new ECDH
    key waits for a person (`ecdh_approved_at` cleared): which key receives an
    agent's vault key is a trust decision a person makes, not one the box makes
    by re-posting. The SIGNING key keeps its status — an active box goes on
    signing in and running every agent that needs no vault key; it only cannot
    read zero-knowledge agents until the new key is approved. Wraps to the old
    ECDH key are dropped (they name its thumbprint). Re-sending the same key, or
    none, changes nothing; a revoked key stays revoked."""
    if jwk is None or jkt == key.ecdh_jkt or key.status == RunnerDeviceKey.REVOKED:
        return
    from apps.agents.credentials import zk

    with transaction.atomic():
        key.ecdh_public_jwk, key.ecdh_jkt = jwk, jkt
        key.ecdh_approved_at, key.ecdh_approved_by = None, None
        key.save(update_fields=["ecdh_public_jwk", "ecdh_jkt", "ecdh_approved_at", "ecdh_approved_by"])
        zk.forget_device_wraps(key)
    if key.status == RunnerDeviceKey.ACTIVE:
        audit.note_change(scope=f"runner-device-key:{key.runner.name}/{key.jkt[:12]}/ecdh",
                          before="approved", after="waiting")
        device_key_notify.notify_pending_on_commit(key, reapproval=True)


def verified_role(request: HttpRequest, payload: RunnerDeviceKeyIn) -> tuple[str, str]:
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
    if RunnerFleet.objects.filter(role_jkt=aws_iam_proof.role_jkt(claimed)).exists():
        # A fleet's role is nobody's key: each member joins as its own instance.
        raise HttpError(422, f"{claimed} is a runner fleet's role; its instances join by pair-by-role")
    return claimed, aws_iam_proof.role_jkt(claimed)


@router.get("/", response=list[RunnerDeviceKeyOut], summary="List runner device keys")
@gate("runner-admin")
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
    agents = vault_agents_of(request.user)
    out = []
    for k in qs:
        row = key_out(k, can_manage=may_trust_device_keys(request.user, k.runner))
        row.can_open = agents_key_opens(k, agents)
        # What it waits on a vault-key wrap for: for a key not yet approved, what
        # approving it will leave waiting, so the browser approves AND wraps in one
        # passkey touch (canopy-web#1550); for an approved key, what it came to
        # serve since (a runner made private, a new license) — "Give access".
        row.zk_pending_agents = zk_agents_waiting_on(request.user, k, assume_active=not k.ecdh_ready)
        out.append(row)
    return out


@router.post("/{key_id}/approve", response=RunnerDeviceKeyOut,
             summary="Approve a pending device key (browser session + passkey step-up)")
@gate("runner-admin", setting=True, step_up=True)  # browser + passkey step-up; private runner: owner only
def approve_runner_device_key(request: HttpRequest, key_id: uuid.UUID):
    """`pending` → `active`, and its ECDH key approved with it; on an active key
    whose new ECDH key waits, approves just that. A revoked key cannot be
    revived (409); approving a fully approved key is a no-op."""
    if not _is_browser_session(request):
        raise HttpError(403, "approving a device key needs a browser session, not a token")
    key = _key_or_404(request, key_id)
    # Asked at approval, not only at registration: a key a runner admin registered
    # while the box was shared stays pending once it is made private, and must not
    # then be approved by them.
    _may_trust_or_403(request, key.runner)
    # A fresh passkey confirmation (canopy-web#1538): an active key unlocks an
    # agent's secrets for that box, so a stolen session cookie must not suffice.
    # 403 `step_up_required` → the SPA prompts for the passkey and retries.
    stepup.require_recent_step_up(request)
    if key.status == RunnerDeviceKey.REVOKED:
        raise HttpError(409, "this key was revoked; register a new one")
    before = key.status
    now = timezone.now()
    fields = []
    if key.status == RunnerDeviceKey.PENDING:
        key.status, key.approved_by, key.approved_at = RunnerDeviceKey.ACTIVE, request.user, now
        fields += ["status", "approved_by", "approved_at"]
    if key.ecdh_jkt and key.ecdh_approved_at is None:
        key.ecdh_approved_by, key.ecdh_approved_at = request.user, now
        fields += ["ecdh_approved_by", "ecdh_approved_at"]
    if fields:
        key.save(update_fields=fields)
    if key.kind == RunnerDeviceKey.AWS_IAM and key.role_arn:
        # A person just trusted this role as the box: it is the runner's machine.
        set_runner_machine(key.runner, key.role_arn)
    audit.note_change(scope=f"runner-device-key:{key.runner.name}/{key.jkt[:12]}",
                      before=before, after=key.status)
    out = key_out(key, can_manage=True)
    out.zk_pending_agents = zk_agents_waiting_on(request.user, key)
    return out


def zk_agents_waiting_on(user, key: RunnerDeviceKey, *, assume_active: bool = False) -> list[str]:
    """Zero-knowledge agents `user` administers that this (now active) key
    should be granted the vault key of — the "Grant" offer after approving.
    `assume_active`: as if it were approved (the list, for a pending key).

    An agent born zero-knowledge (kv 0) has no vault key to grant, only values
    sealed to whoever could read it when each was saved. It is listed when one
    of those values was not sealed to this key: the browser then creates the
    agent's vault key in the same passkey touch (opening the sealed values with
    the person's own key), so a machine approved later can read them too —
    nobody has to "finish setting up" an agent by hand."""
    if not key.ecdh_jkt or key.status == RunnerDeviceKey.REVOKED or not (assume_active or key.ecdh_ready):
        return []
    from apps.agents.credentials import zk

    return sorted(
        agent.slug for agent in Agent.objects.filter(zk_enabled=True).select_related("workspace", "owner")
        if agent.is_admin(user) and device_keys.runner_serves_agent(key.runner, agent)
        and zk.device_waits(agent, key)
    )


def vault_agents_of(user) -> list:
    """The agents whose secrets live in the canopy-web vault that `user`
    administers — what a key's `can_open` is drawn from."""
    return [a for a in Agent.objects.filter(zk_enabled=True, credential_source="canopy-web")
            .select_related("workspace", "owner").order_by("name", "slug") if a.is_admin(user)]


def agents_key_opens(key: RunnerDeviceKey, agents: list) -> list[DeviceKeyAgentOut]:
    """Of `agents`, the ones this key can open right now: its decryption key is
    approved, the box serves the agent, and nothing of the agent's is still
    waiting on it (`zk.device_waits`) — "can open Hal, Eva" on Settings."""
    if not key.ecdh_ready:
        return []
    from apps.agents.credentials import zk

    return [DeviceKeyAgentOut(slug=a.slug, name=a.name or a.slug) for a in agents
            if device_keys.runner_serves_agent(key.runner, a) and not zk.device_waits(a, key)]


@router.delete("/{key_id}", response=RunnerDeviceKeyOut, summary="Revoke a device key")
@gate("runner-admin", setting=True)
def revoke_runner_device_key(request: HttpRequest, key_id: uuid.UUID):
    """Immediate: the next proof signed by this key is refused. Idempotent."""
    key = device_keys.revoke(_key_or_404(request, key_id), by=request.user)
    return key_out(key, can_manage=may_trust_device_keys(request.user, key.runner))

"""Laptop pairing by browser — a downloaded runner pairs with no PAT, ever.

The installed box makes its device key first, then (spec
`docs/superpowers/specs/2026-10-19-runner-installers-design.md`, "Pairing API"):

* `POST /api/harness/runner-pairings/` — anonymous, with a DPoP proof of that
  key: opens a pairing and gets a `user_code` and the `/pair/<code>` page to
  open. A key that is already an active runner's re-attaches instead.
* `GET /api/harness/runner-pairings/{user_code}` — a signed-in person sees what
  the box says it is, and the workspaces they may put it in. The code is the
  capability.
* `POST …/{user_code}/approve` — that person, from a BROWSER behind a fresh
  passkey step-up (a token is refused), picks a workspace and a name. The answer
  is a redirect to the box's own loopback carrying a one-time secret: whoever
  approves must be at the machine, so a phished approve link is useless.
* `POST …/{user_code}/decline`.
* `POST /api/harness/runner-pairings/{id}/claim` — anonymous, with a DPoP proof
  of the same key and the secret: makes the Runner (owned by the approver) and
  its ACTIVE device key. Then the box signs in at `POST /api/runner-sessions/`.

A box whose token stopped working names the runner it already is
(`reconnect_runner`). When that runner's OWNER approves, the claim adds the key to
it — same runner, nothing new — so a dead PAT is fixed from the browser.
"""
from __future__ import annotations

import hmac
import logging
import re
import secrets
import uuid

from django.db import IntegrityError, transaction
from django.http import HttpRequest
from django.utils import timezone
from ninja import Router, Status
from ninja.errors import HttpError

from apps.common.token_hash import hash_token
from apps.api.auth import session_auth
from apps.api.errors import TYPE_AUTH, ProblemError
from apps.events import activity
from apps.passkeys import stepup
from apps.workspaces import links as wslinks
from apps.workspaces import permissions as perms
from apps.workspaces import services as wsvc
from apps.workspaces.models import Workspace

from . import device_keys
from .api_device_keys import ECDH_STORES
from .models import PAIRING_APPROVAL_GRACE, Runner, RunnerDeviceKey, RunnerPairing
from .schemas import (
    RunnerPairedOut,
    RunnerPairingApprovedOut,
    RunnerPairingApproveIn,
    RunnerPairingClaimIn,
    RunnerPairingCreatedOut,
    RunnerPairingDeclinedOut,
    RunnerPairingIn,
    RunnerPairingOut,
    RunnerPairingWorkspaceOut,
)

logger = logging.getLogger(__name__)

router = Router(auth=session_auth, tags=["harness"])

#: Seconds between the box's claim polls.
POLL_INTERVAL = 3
#: No 0/O, 1/I/L: the code is read off one screen and checked against another.
_CODE_ALPHABET = "ABCDEFGHJKMNPQRSTUVWXYZ23456789"

_REFUSED = "no pairing for this device key can be completed with this request"


def _refuse(why: str) -> ProblemError:
    # One answer for every refusal on the anonymous doors — a bad proof, another
    # key, declined, expired, used, a wrong secret: a caller learns nothing about
    # which pairings, keys or runners exist. The reason goes to the log.
    logger.info("runner pairing refused: %s", why)
    return ProblemError(401, "Pairing refused", type_=TYPE_AUTH, detail=_REFUSED)


def _new_user_code() -> str:
    raw = "".join(secrets.choice(_CODE_ALPHABET) for _ in range(8))
    return f"{raw[:4]}-{raw[4:]}"


def _normalize_code(code: str) -> str:
    """`abcd efgh`, `ABCDEFGH` and `ABCD-EFGH` are the same code."""
    raw = re.sub(r"[^A-Za-z0-9]", "", code or "").upper()
    return f"{raw[:4]}-{raw[4:]}" if len(raw) == 8 else raw


def _secret_hash(raw: str) -> str:
    return hash_token(raw)


def verify_path(pairing: RunnerPairing) -> str:
    # Root, not under /w/: a pairing has no workspace until the person picks one.
    return f"/pair/{pairing.user_code}"


def suggested_name(account: str, hostname: str) -> str:
    """`<account>-<hostname>` as a runner name (`RUNNER_NAME_PATTERN`)."""
    host = re.sub(r"\.local$", "", (hostname or "").strip(), flags=re.IGNORECASE)
    name = "-".join(p for p in ((account or "").strip(), host) if p).lower()
    name = re.sub(r"[^a-z0-9-]+", "-", name)
    name = re.sub(r"-{2,}", "-", name)
    name = re.sub(r"^[^a-z]+", "", name)[:63].rstrip("-")
    return name or "runner"


def _proven_jkt(request: HttpRequest) -> str:
    try:
        return device_keys.verify_proof(request)
    except device_keys.ProofError as exc:
        raise _refuse(f"proof: {exc}") from exc


def _paired_out(status: str, key: RunnerDeviceKey) -> RunnerPairedOut:
    return RunnerPairedOut(status=status, runner_id=key.runner_id, name=key.runner.name,
                           workspace=key.runner.workspace_id or "", device_key_id=key.pk)


# ---- the box: open a pairing, then claim it (anonymous; public in middleware) ---------

@router.post("/", response={200: RunnerPairedOut, 201: RunnerPairingCreatedOut}, auth=None,
             summary="A new laptop box asks to be paired by browser (no token)")
def create_runner_pairing(request: HttpRequest, payload: RunnerPairingIn):
    """Carry a DPoP proof (bound to this URL) by the key in `public_jwk`. 201:
    a pairing — show `user_code`, open `verify_url`, and poll the claim every
    `interval` seconds until `expires_at` (15 minutes). A key that is already an
    active runner's: 200 `reattached`, and nothing is made. A proof by another
    key, or a key canopy knows in any other state: one uniform 401."""
    jkt = _proven_jkt(request)
    try:
        jwk = device_keys.normalize_public_jwk(payload.public_jwk)
    except device_keys.KeyError_ as exc:
        raise _refuse(f"public_jwk: {exc}") from exc
    if device_keys.thumbprint(jwk) != jkt:
        raise _refuse("the proof was signed by a different key than public_jwk")
    ecdh: dict = {}
    if payload.ecdh_public_jwk:
        try:
            ecdh = device_keys.normalize_public_jwk(payload.ecdh_public_jwk)
        except device_keys.KeyError_ as exc:
            raise HttpError(422, f"ecdh_public_jwk: {exc}") from exc

    existing = RunnerDeviceKey.objects.select_related("runner").filter(jkt=jkt).first()
    if existing is not None:
        if existing.status == RunnerDeviceKey.ACTIVE and existing.runner.status != Runner.RETIRED:
            # A reinstall on a box whose key survived: it is already that runner.
            return Status(200, _paired_out("reattached", existing))
        # A revoked or pending key, or a retired runner's, is never re-paired
        # silently: revoking was someone's decision, and a fresh key pairs fresh.
        raise _refuse(f"key {existing.pk} is {existing.status} on runner {existing.runner.name}")

    now = timezone.now()
    for _ in range(5):
        try:
            with transaction.atomic():
                RunnerPairing.objects.filter(expires_at__lte=now, consumed_at__isnull=True).delete()
                # One live pairing per key: a new one replaces it.
                RunnerPairing.objects.filter(jkt=jkt, consumed_at__isnull=True).delete()
                pairing = RunnerPairing.objects.create(
                    user_code=_new_user_code(), public_jwk=jwk, jkt=jkt, kind=payload.kind,
                    ecdh_public_jwk=ecdh,
                    ecdh_store=payload.ecdh_store if ecdh and payload.ecdh_store in ECDH_STORES else "",
                    hostname=payload.hostname.strip(), account=payload.account.strip(),
                    platform=payload.platform, has_emdash=payload.has_emdash,
                    redirect_port=payload.redirect_port, created_at=now,
                    reconnect_runner=_live_runner(payload.reconnect_runner),
                )
            break
        except IntegrityError:  # a user_code collision; draw again
            continue
    else:  # pragma: no cover — 31^8 codes
        raise HttpError(503, "could not allocate a pairing code; try again")
    return Status(201, RunnerPairingCreatedOut(
        id=pairing.pk, user_code=pairing.user_code, verify_url=wslinks.public_url(verify_path(pairing)),
        expires_at=pairing.expires_at, interval=POLL_INTERVAL))


@router.post("/{pairing_id}/claim", response={201: RunnerPairedOut, 202: RunnerPairedOut}, auth=None,
             summary="The box claims its approved pairing (no token)")
def claim_runner_pairing(request: HttpRequest, pairing_id: uuid.UUID, payload: RunnerPairingClaimIn):
    """Carry a DPoP proof (bound to this URL) by the pairing's key. 202
    `waiting` until a person approves; then, with the `secret` the approval
    handed to this box's loopback, 201 `paired`: a new runner owned by the
    approver, in the workspace they chose, whose device key is already ACTIVE.
    Anything else — declined, expired, used, a wrong secret or key, an approver
    who may no longer run work there — one uniform 401."""
    from .api import declared_capabilities

    jkt = _proven_jkt(request)
    pairing = RunnerPairing.objects.select_related("approved_by").filter(pk=pairing_id).first()
    if pairing is None or pairing.jkt != jkt:
        raise _refuse(f"no pairing {pairing_id} for this key")
    status = pairing.status
    if status == RunnerPairing.PENDING:
        return Status(202, RunnerPairedOut(status="waiting"))
    if status != RunnerPairing.APPROVED:
        raise _refuse(f"pairing {pairing.pk} is {status}")
    if not payload.secret or not pairing.secret_hash \
            or not hmac.compare_digest(_secret_hash(payload.secret), pairing.secret_hash):
        raise _refuse(f"pairing {pairing.pk}: wrong secret")
    owner = pairing.approved_by
    # An approval is not a standing grant that outlives the approver's access.
    if owner is None or not owner.is_active \
            or not perms.can(owner, pairing.workspace_id, perms.AGENT_WORK):
        raise _refuse(f"pairing {pairing.pk}: its approver may no longer run work in {pairing.workspace_id}")

    try:
        with transaction.atomic():
            locked = (RunnerPairing.objects.select_for_update()
                      .filter(pk=pairing.pk, consumed_at__isnull=True, secret_hash=pairing.secret_hash)
                      .first())
            if locked is None:  # a concurrent claim, or a re-approval with a new secret
                raise _refuse(f"pairing {pairing.pk} changed under the claim")
            # A box whose token stopped working gets its OWN runner back (approved by
            # that runner's owner): same id, routing, sessions — only a key is added.
            runner = _reconnect_for(locked, owner)
            if runner is None:
                runner = Runner.objects.create(
                    name=locked.name, kind=Runner.EMDASH,
                    engine=Runner.ENGINE_EMDASH if locked.has_emdash else Runner.ENGINE_CLAUDE_CLI,
                    capabilities=declared_capabilities({"sessions": True}),
                    host=locked.hostname, owner=owner, workspace_id=locked.workspace_id,
                )
            ecdh_jkt = device_keys.thumbprint(locked.ecdh_public_jwk) if locked.ecdh_public_jwk else ""
            key = RunnerDeviceKey.objects.create(
                runner=runner, kind=locked.kind, public_jwk=locked.public_jwk, jkt=locked.jkt,
                label=f"{locked.hostname or locked.name} ({locked.kind})", status=RunnerDeviceKey.ACTIVE,
                created_by=owner, approved_by=owner, approved_at=locked.approved_at,
                # The ECDH key the approver saw is approved with it.
                ecdh_public_jwk=locked.ecdh_public_jwk, ecdh_jkt=ecdh_jkt, ecdh_store=locked.ecdh_store,
                ecdh_approved_at=locked.approved_at if ecdh_jkt else None,
                ecdh_approved_by=owner if ecdh_jkt else None,
            )
            locked.consumed_at, locked.runner = timezone.now(), runner
            locked.save(update_fields=["consumed_at", "runner"])
    except IntegrityError:
        # The key became another runner's meanwhile (unique jkt).
        raise _refuse(f"pairing {pairing.pk}: a concurrent pairing of this key won") from None
    activity.emit(activity.RUNNER_PAIRED, workspace=pairing.workspace_id, actor=owner, surface="runner",
                  ref=str(runner.pk), meta={"name": runner.name, "pairing": str(pairing.pk)})
    return Status(201, _paired_out("paired", key))


# ---- the person: see, approve or decline (browser session) ---------------------------

def _live_runner(runner_id):
    if runner_id is None:
        return None
    return Runner.objects.filter(pk=runner_id).exclude(status=Runner.RETIRED).first()


def _reconnect_for(pairing: RunnerPairing, user):
    """The runner this pairing reconnects, when `user` owns it and it is live — else
    None (a fresh pairing). The box only NAMES a runner; owning it is what counts."""
    runner = pairing.reconnect_runner
    if (runner is None or user is None or runner.owner_id != user.pk or runner.status == Runner.RETIRED
            or runner.kind == Runner.COMPUTER):
        return None
    return runner


def _pairing_or_404(user_code: str) -> RunnerPairing:
    pairing = RunnerPairing.objects.filter(user_code=_normalize_code(user_code)).first()
    if pairing is None:
        raise HttpError(404, "pairing not found")
    return pairing


def _workspaces_for(user) -> list[RunnerPairingWorkspaceOut]:
    slugs = [s for s in wsvc.user_workspace_slugs(user) if perms.can(user, s, perms.AGENT_WORK)]
    return [RunnerPairingWorkspaceOut(slug=w.slug, name=w.display_name or w.slug)
            for w in Workspace.objects.filter(slug__in=slugs).order_by("slug")]


@router.get("/{user_code}", response=RunnerPairingOut, summary="A runner pairing, by its code")
def get_runner_pairing(request: HttpRequest, user_code: str):
    """What the box says it is, where the pairing stands, and the workspaces you
    may put it in (those where you may run work). Anyone signed in who has the
    code may look; 404 for a code that does not exist."""
    pairing = _pairing_or_404(user_code)
    return RunnerPairingOut(
        user_code=pairing.user_code, hostname=pairing.hostname, account=pairing.account,
        platform=pairing.platform, kind=pairing.kind, has_emdash=pairing.has_emdash,
        status=pairing.status, expires_at=pairing.expires_at,
        suggested_name=pairing.name or suggested_name(pairing.account, pairing.hostname),
        workspaces=_workspaces_for(request.user),
        runner_id=pairing.runner_id if pairing.approved_by_id == request.user.pk else None,
        reconnect_name=getattr(_reconnect_for(pairing, request.user), "name", ""),
    )


def _refuse_finished(pairing: RunnerPairing) -> None:
    status = pairing.status
    if status == RunnerPairing.CONSUMED:
        raise HttpError(409, "this pairing has already been used: that computer is paired")
    if status == RunnerPairing.DECLINED:
        raise HttpError(409, "this pairing was declined; start pairing again from Canopy Runner")
    if status == RunnerPairing.EXPIRED:
        raise HttpError(409, "this pairing has expired; start pairing again from Canopy Runner")


@router.post("/{user_code}/approve", response=RunnerPairingApprovedOut,
             summary="Approve a runner pairing (browser session + passkey step-up)")
def approve_runner_pairing(request: HttpRequest, user_code: str, payload: RunnerPairingApproveIn):
    """Make the box your runner, named `name`, in `workspace` (one where you may
    run work). Only from a browser after a fresh passkey touch; a token is
    refused. The answer's `redirect_url` is the box's own loopback with a
    one-time secret — send this browser there. Approving again before the box
    claims issues a new secret. Used, declined or expired: 409."""
    pairing = _pairing_or_404(user_code)
    # A fresh passkey confirmation, and never a token: approving lets a machine
    # into the fleet as you. 403 `step_up_required` → the SPA asks for the
    # passkey and retries; a token gets a plain 403 it cannot answer.
    stepup.require_recent_step_up(request)
    # A runner is owned by a person (apps/harness/api_runners.py::pair_runner).
    if getattr(request.user, "agent_identity", None) is not None:
        raise HttpError(403, "a runner must be owned by a person; approve it as yourself")
    reconnect = _reconnect_for(pairing, request.user)
    slug = reconnect.workspace_id if reconnect is not None else payload.workspace.strip()
    name = reconnect.name if reconnect is not None else payload.name
    if not wsvc.is_member(request.user, slug):
        raise HttpError(404, f"workspace '{slug}' not found")
    if not perms.can(request.user, slug, perms.AGENT_WORK):
        raise HttpError(403, "pairing a runner into a workspace needs permission to run work there")
    _refuse_finished(pairing)
    # A computer's key holder shares its machine's name and is no runner app.
    if reconnect is None and (Runner.objects.filter(owner=request.user, name=name)
                              .exclude(status=Runner.RETIRED).exclude(kind=Runner.COMPUTER).exists()):
        raise HttpError(409, f"you already have a runner named '{name}'; choose another name")
    raw = secrets.token_urlsafe(32)
    now = timezone.now()
    first = pairing.approved_at is None
    pairing.workspace_id, pairing.name = slug, name
    pairing.approved_by, pairing.approved_at = request.user, now
    pairing.secret_hash = _secret_hash(raw)
    if first:
        pairing.expires_at = pairing.expires_at + PAIRING_APPROVAL_GRACE
    pairing.save(update_fields=["workspace", "name", "approved_by", "approved_at", "secret_hash",
                                "expires_at"])
    activity.emit(activity.RUNNER_PAIRING_APPROVED, workspace=slug, actor=request.user, surface="runner",
                  ref=str(pairing.pk), meta={"name": name, "hostname": pairing.hostname,
                                             "platform": pairing.platform})
    return RunnerPairingApprovedOut(
        status="approved",
        redirect_url=f"http://127.0.0.1:{pairing.redirect_port}/paired?pairing={pairing.pk}&secret={raw}")


@router.post("/{user_code}/decline", response=RunnerPairingDeclinedOut, summary="Decline a runner pairing")
def decline_runner_pairing(request: HttpRequest, user_code: str):
    """This is not your computer, or not one you meant to pair: the box's claim
    is refused from now on. Used or expired: 409."""
    pairing = _pairing_or_404(user_code)
    if pairing.status != RunnerPairing.DECLINED:
        _refuse_finished(pairing)
        pairing.declined_at = timezone.now()
        pairing.save(update_fields=["declined_at"])
    return RunnerPairingDeclinedOut(status="declined")

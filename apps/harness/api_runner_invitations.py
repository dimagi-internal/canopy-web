"""Pairing invitations — a new cloud box pairs holding no long-lived secret.

A cloud box used to carry a shared person's PAT whose only job was to get it
through pairing; when it expired, nothing said so. Now (spec
`docs/superpowers/specs/2026-10-19-cloud-runner-standup-friction-design.md` #9):

* `POST /api/harness/runner-invitations/` `{name, workspace, role_arn, ecdh_public_jwk?}`
  — the operator (`up.sh`, with their own token) invites ONE box, by the name it
  will pair as and its EC2 instance role, into a workspace where they may run
  work. A live invitation for the same role and name is replaced. Its creator is
  mailed and pushed `approve_url`.
* `GET /api/harness/runner-invitations/{id}` — its state, and once the box has
  paired, the runner's id. Creator only.
* `POST /api/harness/runner-invitations/{id}/approve` — the creator, from a
  BROWSER, behind a fresh passkey step-up. A token cannot (403), so neither
  `up.sh` nor the box can approve its own invitation.

A box of isolation units (spec 2026-10-19-runner-isolation-units §2) is invited
with `units`, and its one approval pairs every unit: `pair-by-role` with `units`
is `unit_pairing.pair_units`.

Who approves: only the creator, who becomes the runner's owner — as only a box's
owner or its runner admins approve a device key, and no runner admin can exist
before the runner does.
"""
from __future__ import annotations

import logging
import uuid

from django.db import IntegrityError, transaction
from django.http import HttpRequest
from django.utils import timezone
from ninja import Router, Status
from ninja.errors import HttpError

from apps.api.auth import session_auth
from apps.api.errors import TYPE_AUTH, ProblemError
from apps.events import activity
from apps.passkeys import stepup
from apps.workspaces import permissions as perms
from apps.workspaces import services as wsvc
from apps.workspaces import links as wslinks

from . import aws_iam_proof, device_key_notify, device_keys, unit_pairing
from .api_device_keys import ECDH_STORES
from .models import Runner, RunnerDeviceKey, RunnerInvitation
from .runners import set_runner_machine
from .schemas import (RunnerInvitationCheckOut, RunnerInvitationIn, RunnerInvitationOut,
                      RunnerInvitationUnitOut, RunnerPairByRoleIn, RunnerPairByRoleOut)

logger = logging.getLogger(__name__)

router = Router(auth=session_auth, tags=["harness"])

# Says nothing about who holds it.
_INVITATION_TAKEN = "a live invitation for this instance role and runner name already exists"


def approve_path(invitation: RunnerInvitation) -> str:
    return f"/runner-invitations/{invitation.pk}"


def approve_url(invitation: RunnerInvitation) -> str:
    """The approve page, under the invitation's workspace."""
    return wslinks.scoped_url(invitation.workspace_id, approve_path(invitation))


def invitation_out(invitation: RunnerInvitation) -> RunnerInvitationOut:
    units = unit_pairing.units_of(invitation)
    return RunnerInvitationOut(
        id=invitation.pk, name=invitation.name, workspace=invitation.workspace_id,
        role_arn=invitation.role_arn, status=invitation.status, approve_url=approve_url(invitation),
        created_at=invitation.created_at, expires_at=invitation.expires_at,
        created_by_email=invitation.created_by.email or "", approved_at=invitation.approved_at,
        has_ecdh_key=bool(invitation.ecdh_public_jwk) or any(u.get("ecdh_public_jwk") for u in units),
        runner_id=invitation.runner_id,
        units=[RunnerInvitationUnitOut(
            name=u["name"], workspace=u["workspace"], kind=u.get("kind") or "tree",
            slug=u.get("slug") or "", has_ecdh_key=bool(u.get("ecdh_public_jwk")),
            runner_id=u.get("runner_id") or None) for u in units],
    )


def _invited_units(request: HttpRequest, payload: RunnerInvitationIn) -> list[dict]:
    """The units a box of isolation units will pair as, each checked as the box's
    own workspace is: the inviter may run work in its workspace, an agent unit
    names an agent of that workspace, and no name is used twice. [] for a box that
    pairs as one runner."""
    if not payload.units:
        return []
    from apps.agents.models import Agent

    out: list[dict] = []
    seen: set[str] = set()
    for unit in payload.units:
        if unit.name in seen:
            raise HttpError(422, f"units: '{unit.name}' is named twice")
        seen.add(unit.name)
        slug = _may_invite_into(request, unit.workspace)
        agent = ""
        if unit.kind == "agent":
            agent = (unit.slug or "").strip()
            if not agent or not Agent.objects.filter(slug=agent, workspace_id=slug).exists():
                raise HttpError(422, f"units: '{unit.name}' is an agent unit, and '{agent}' is not an "
                                     f"agent of {slug}")
        ecdh: dict = {}
        if unit.ecdh_public_jwk:
            try:
                ecdh = device_keys.normalize_public_jwk(unit.ecdh_public_jwk)
            except device_keys.KeyError_ as exc:
                raise HttpError(422, f"units: '{unit.name}' ecdh_public_jwk: {exc}") from exc
        out.append({"name": unit.name, "workspace": slug, "kind": unit.kind, "slug": agent,
                    "ecdh_public_jwk": ecdh, "runner_id": None})
    return out


def _own_invitation_or_404(request: HttpRequest, invitation_id: uuid.UUID) -> RunnerInvitation:
    invitation = (RunnerInvitation.objects.select_related("created_by")
                  .filter(pk=invitation_id, created_by=request.user).first())
    # 404 for someone else's, like every harness route: do not confirm it exists.
    if invitation is None:
        raise HttpError(404, "invitation not found")
    return invitation


def _may_invite_into(request: HttpRequest, workspace: str) -> str:
    """The workspace slug, if the caller may invite a runner into it; else the
    refusal. Shared by the create and its pre-check, so they cannot disagree."""
    slug = (workspace or "").strip()
    if not slug or not wsvc.is_member(request.user, slug):
        raise HttpError(404, f"workspace '{slug}' not found")
    if not perms.can(request.user, slug, perms.AGENT_WORK):
        raise HttpError(403, "inviting a runner into a workspace needs permission to run work there")
    # A runner is owned by a person (apps/harness/api.py::pair_runner): an
    # agent's login inviting a box would make a runner nobody can manage.
    if getattr(request.user, "agent_identity", None) is not None:
        raise HttpError(403, "a runner must be owned by a person; invite it with your own token")
    return slug


# Declared before `/{invitation_id}`, which would match it.
@router.get("/check", response=RunnerInvitationCheckOut,
            summary="May you invite a runner into this workspace? (changes nothing)")
def check_runner_invitation(request: HttpRequest, workspace: str):
    """The same gates as creating an invitation, without creating one: 404 not a
    member, 403 no permission to run work there. `up.sh` asks before it writes
    anything to AWS, since the box's role (which an invitation names) exists
    only after the stack does."""
    return RunnerInvitationCheckOut(workspace=_may_invite_into(request, workspace))


@router.post("/", response={201: RunnerInvitationOut},
             summary="Invite a new cloud runner to pair by its instance role")
def create_runner_invitation(request: HttpRequest, payload: RunnerInvitationIn):
    """Invite one box, named, bound to its EC2 instance role, into a workspace
    where you may run work. It pairs only once you approve it at `approve_url`
    with a passkey, and only within an hour. Inviting the same role and name
    again replaces the earlier invitation."""
    slug = _may_invite_into(request, payload.workspace)
    try:
        role_arn = aws_iam_proof.canonical_role_arn(payload.role_arn)
    except aws_iam_proof.AwsProofError as exc:
        raise HttpError(422, f"role_arn: {exc}") from exc
    ecdh: dict = {}
    if payload.ecdh_public_jwk:
        try:
            ecdh = device_keys.normalize_public_jwk(payload.ecdh_public_jwk)
        except device_keys.KeyError_ as exc:
            raise HttpError(422, f"ecdh_public_jwk: {exc}") from exc
    jkt = aws_iam_proof.role_jkt(role_arn)
    units = _invited_units(request, payload)
    try:
        with transaction.atomic():
            live = (RunnerInvitation.objects.select_for_update()
                    .filter(role_jkt=jkt, name=payload.name, consumed_at__isnull=True))
            # Replace only what is yours, or what has run out. Someone else's live
            # invitation for this role and name is theirs: replacing it would let
            # you take over the box they invited (and may already have approved).
            if live.exclude(created_by=request.user).filter(expires_at__gt=timezone.now()).exists():
                raise HttpError(409, _INVITATION_TAKEN)
            live.delete()
            invitation = RunnerInvitation.objects.create(
                name=payload.name, workspace_id=slug, created_by=request.user,
                role_arn=role_arn, role_jkt=jkt, ecdh_public_jwk=ecdh, units=units,
            )
    except IntegrityError:  # a concurrent create for the same role and name won
        raise HttpError(409, _INVITATION_TAKEN) from None
    activity.emit(activity.RUNNER_INVITED, workspace=slug, actor=request.user, surface="runner",
                  ref=str(invitation.pk), meta={"name": invitation.name})
    device_key_notify.notify_invitation_on_commit(invitation, approve_url(invitation))
    return Status(201, invitation_out(invitation))


@router.get("/{invitation_id}", response=RunnerInvitationOut, summary="A pairing invitation and its state")
def get_runner_invitation(request: HttpRequest, invitation_id: uuid.UUID):
    """`pending` until you approve it, `approved` until the box pairs, then
    `consumed` with the runner's id; `expired` an hour after it was made."""
    return invitation_out(_own_invitation_or_404(request, invitation_id))


@router.post("/{invitation_id}/approve", response=RunnerInvitationOut,
             summary="Approve a pairing invitation (browser session + passkey step-up)")
def approve_runner_invitation(request: HttpRequest, invitation_id: uuid.UUID):
    """Let the invited box pair: it will be your runner, in the invitation's
    workspace, signing in with its instance role. Only the invitation's creator,
    from a browser, after a fresh passkey touch; a token is refused. An expired or
    already-used invitation cannot be approved (409); approving twice is a no-op."""
    invitation = _own_invitation_or_404(request, invitation_id)
    # A fresh passkey confirmation, and never a token: approving lets a machine
    # into the fleet as you. 403 `step_up_required` → the SPA asks for the
    # passkey and retries; a token gets a plain 403 it cannot answer.
    stepup.require_recent_step_up(request)
    if invitation.status == RunnerInvitation.CONSUMED:
        raise HttpError(409, "this invitation has already been used")
    if invitation.status == RunnerInvitation.EXPIRED:
        raise HttpError(409, "this invitation has expired; run up.sh again for a new one")
    if invitation.approved_at is None:
        invitation.approved_at, invitation.approved_by = timezone.now(), request.user
        invitation.save(update_fields=["approved_at", "approved_by"])
        activity.emit(activity.RUNNER_INVITATION_APPROVED, workspace=invitation.workspace_id,
                      actor=request.user, surface="runner", ref=str(invitation.pk),
                      meta={"name": invitation.name, "role_arn": invitation.role_arn})
    return invitation_out(invitation)


# ---- the box pairs: `POST /api/harness/runners/pair-by-role` --------------------------
#
# Anonymous at the door (`auth=None`, and the path is public in
# `apps/common/middleware.py`): the box holds no token. Its instance role's proof
# is the identity, and an invitation its creator APPROVED behind a passkey is the
# authority. Nothing the box sends can approve anything: it is let in by what a
# person already decided, or it waits.

pair_router = Router(auth=None, tags=["harness"])

_PAIR_REFUSED = "no approved pairing invitation matches this box's instance role and name"


def _refuse_pairing(why: str) -> ProblemError:
    # One answer for every refusal — no invitation, expired, used, another name,
    # another ECDH key, a bad proof: a caller learns nothing about which
    # invitations, runners or roles exist. The reason goes to the log.
    logger.info("pair-by-role refused: %s", why)
    return ProblemError(401, "Pairing refused", type_=TYPE_AUTH, detail=_PAIR_REFUSED)


def _ecdh_of(payload: RunnerPairByRoleIn) -> tuple[dict, str]:
    if not payload.ecdh_public_jwk:
        return {}, ""
    try:
        jwk = device_keys.normalize_public_jwk(payload.ecdh_public_jwk)
    except device_keys.KeyError_ as exc:
        raise _refuse_pairing(f"ecdh_public_jwk: {exc}") from exc
    return jwk, device_keys.thumbprint(jwk)


@pair_router.post("", response={200: RunnerPairByRoleOut, 201: RunnerPairByRoleOut, 202: RunnerPairByRoleOut},
                  auth=None, summary="A new cloud box pairs by its instance role (no token)")
def pair_runner_by_role(request: HttpRequest, payload: RunnerPairByRoleIn):
    """Prove this box's EC2 instance role (`proof`, bound to this URL). With an
    APPROVED, unexpired, unused invitation for that role and `name`: 201
    `paired` — a new runner owned by the invitation's creator, in its
    workspace, whose instance role is already its approved device key. A role
    whose runner already exists under that name: 200 `reattached` (a replaced
    instance; nothing is made). An invitation not yet approved: 202 `waiting`.
    Anything else: one uniform 401. Then sign in at `POST /api/runner-sessions/`.

    A box of isolation units sends `units` (each unit's name, the public half of
    its own signing key and its ECDH key) with ONE proof: one runner per unit with
    an active software device key, `units` mapping each name to its runner id. A
    replaced instance (the same role and unit names) re-attaches every unit, its
    new key replacing the old in place, keeping each runner id; that needs no
    invitation. A unit not yet paired needs the approved invitation that lists it."""
    from .api import declared_capabilities

    try:
        role_arn = aws_iam_proof.verify(payload.proof, request)
    except aws_iam_proof.AwsProofError as exc:
        raise _refuse_pairing(f"proof: {exc}") from exc
    if payload.units:
        # A box of isolation units: every unit, one runner each, in one call.
        return unit_pairing.pair_units(request, payload, role_arn, refuse=_refuse_pairing,
                                       out_cls=RunnerPairByRoleOut, status_cls=Status)
    jkt = aws_iam_proof.role_jkt(role_arn)

    existing = RunnerDeviceKey.objects.select_related("runner").filter(jkt=jkt).first()
    if (existing is not None and existing.status == RunnerDeviceKey.ACTIVE
            and existing.runner.status != Runner.RETIRED):
        # The same role, a new instance (`up.sh --allow-instance-change`): the role
        # IS the box's identity and a person already approved it. Only as the name
        # it was approved for — a role cannot pair as a second runner.
        if existing.runner.name != payload.name:
            raise _refuse_pairing(f"{role_arn} is already {existing.runner.name}, not {payload.name}")
        # The verified role IS the machine (`Runner.machine_id`): a runner paired
        # before the field existed learns it here.
        set_runner_machine(existing.runner, role_arn)
        return Status(200, RunnerPairByRoleOut(status="reattached", runner_id=existing.runner_id))

    invitation = (RunnerInvitation.objects.select_related("created_by")
                  .filter(role_jkt=jkt, name=payload.name, consumed_at__isnull=True).first())
    if invitation is None:
        raise _refuse_pairing(f"no live invitation for {role_arn} as {payload.name}")
    if invitation.expired:
        raise _refuse_pairing(f"invitation {invitation.pk} expired")
    if invitation.approved_at is None:
        return Status(202, RunnerPairByRoleOut(status="waiting"))
    ecdh_jwk, ecdh_jkt = _ecdh_of(payload)
    if invitation.ecdh_public_jwk and ecdh_jkt \
            and ecdh_jkt != device_keys.thumbprint(invitation.ecdh_public_jwk):
        raise _refuse_pairing(f"invitation {invitation.pk}: the box's ECDH key is not the one approved")
    owner = invitation.created_by
    # The creator must still be someone who may run work there: an approval is
    # not a standing grant that outlives their access.
    if not owner.is_active or not perms.can(owner, invitation.workspace_id, perms.AGENT_WORK):
        raise _refuse_pairing(f"invitation {invitation.pk}: its creator may no longer run work in "
                              f"{invitation.workspace_id}")
    if existing is not None:
        # The role already has a key. It is replaced only when it is the SAME
        # person's and its runner was retired. Another owner's key (live,
        # revoked, or a retired box's — whose vault wraps would cascade away) is
        # never this invitation's to erase; and a revoked key on a live runner was
        # a deliberate act its owner undoes, not something re-inviting launders.
        if existing.runner.owner_id != owner.pk:
            raise _refuse_pairing(f"{role_arn}'s key belongs to another owner's runner {existing.runner.name}")
        if existing.runner.status != Runner.RETIRED:
            raise _refuse_pairing(f"{role_arn} has a {existing.status} key on live runner {existing.runner.name}")

    try:
        with transaction.atomic():
            locked = (RunnerInvitation.objects.select_for_update()
                      .filter(pk=invitation.pk, consumed_at__isnull=True).first())
            if locked is None:  # a concurrent pair-by-role consumed it first
                raise _refuse_pairing(f"invitation {invitation.pk} was consumed concurrently")
            if existing is not None:
                # This owner's retired runner's key: the role is re-approved for
                # a new runner, and one role has one key row (unique jkt).
                existing.delete()
            runner = Runner.objects.create(
                name=payload.name, kind=Runner.CLOUD, host=payload.host,
                capabilities=declared_capabilities(payload.capabilities),
                owner=owner, workspace_id=invitation.workspace_id,
                machine_id=aws_iam_proof.canonical_role_arn(role_arn),
            )
            # The ECDH key the invitation named (the `<stack>-ecdh` KMS key, which
            # only this role can use) is approved with it — also when a fresh box
            # cannot report one yet (its canopy CLI arrives with the bootstrap); a
            # later report of the same key changes nothing. A key the invitation did
            # not name waits for a person, as a new ECDH key always does.
            store = payload.ecdh_store if ecdh_jkt and payload.ecdh_store in ECDH_STORES else ""
            if not ecdh_jkt and invitation.ecdh_public_jwk:
                ecdh_jwk, store = invitation.ecdh_public_jwk, "aws-kms"
                ecdh_jkt = device_keys.thumbprint(ecdh_jwk)
            ecdh_approved = bool(ecdh_jkt) and bool(invitation.ecdh_public_jwk)
            RunnerDeviceKey.objects.create(
                runner=runner, kind=RunnerDeviceKey.AWS_IAM, role_arn=role_arn, jkt=jkt,
                label=f"{payload.name} (EC2 instance role)", status=RunnerDeviceKey.ACTIVE,
                created_by=owner, approved_by=invitation.approved_by, approved_at=invitation.approved_at,
                ecdh_public_jwk=ecdh_jwk, ecdh_jkt=ecdh_jkt, ecdh_store=store,
                ecdh_approved_at=invitation.approved_at if ecdh_approved else None,
                ecdh_approved_by=invitation.approved_by if ecdh_approved else None,
            )
            locked.consumed_at, locked.runner = timezone.now(), runner
            locked.save(update_fields=["consumed_at", "runner"])
    except IntegrityError:
        # A concurrent pairing of the same role made its key first (unique jkt).
        raise _refuse_pairing(f"{role_arn}: a concurrent pairing won") from None
    activity.emit(activity.RUNNER_PAIRED, workspace=invitation.workspace_id, actor=owner, surface="runner",
                  ref=str(runner.pk), meta={"name": runner.name, "invitation": str(invitation.pk)})
    return Status(201, RunnerPairByRoleOut(status="paired", runner_id=runner.pk))

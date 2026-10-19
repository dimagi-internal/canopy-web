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

Who approves: only the creator, who becomes the runner's owner — as only a box's
owner or its runner admins approve a device key, and no runner admin can exist
before the runner does.
"""
from __future__ import annotations

import uuid

from django.db import transaction
from django.http import HttpRequest
from django.utils import timezone
from ninja import Router, Status
from ninja.errors import HttpError

from apps.api.auth import session_auth
from apps.events import activity
from apps.passkeys import stepup
from apps.workspaces import permissions as perms
from apps.workspaces import services as wsvc

from . import aws_iam_proof, device_key_notify, device_keys
from .models import RunnerInvitation
from .schemas import RunnerInvitationIn, RunnerInvitationOut

router = Router(auth=session_auth, tags=["harness"])


def approve_path(invitation: RunnerInvitation) -> str:
    return f"/runner-invitations/{invitation.pk}"


def approve_url(invitation: RunnerInvitation) -> str:
    """The approve page, under the invitation's workspace."""
    return wsvc.scoped_url(invitation.workspace_id, approve_path(invitation))


def invitation_out(invitation: RunnerInvitation) -> RunnerInvitationOut:
    return RunnerInvitationOut(
        id=invitation.pk, name=invitation.name, workspace=invitation.workspace_id,
        role_arn=invitation.role_arn, status=invitation.status, approve_url=approve_url(invitation),
        created_at=invitation.created_at, expires_at=invitation.expires_at,
        created_by_email=invitation.created_by.email or "", approved_at=invitation.approved_at,
        has_ecdh_key=bool(invitation.ecdh_public_jwk), runner_id=invitation.runner_id,
    )


def _own_invitation_or_404(request: HttpRequest, invitation_id: uuid.UUID) -> RunnerInvitation:
    invitation = (RunnerInvitation.objects.select_related("created_by")
                  .filter(pk=invitation_id, created_by=request.user).first())
    # 404 for someone else's, like every harness route: do not confirm it exists.
    if invitation is None:
        raise HttpError(404, "invitation not found")
    return invitation


@router.post("/", response={201: RunnerInvitationOut},
             summary="Invite a new cloud runner to pair by its instance role")
def create_runner_invitation(request: HttpRequest, payload: RunnerInvitationIn):
    """Invite one box, named, bound to its EC2 instance role, into a workspace
    where you may run work. It pairs only once you approve it at `approve_url`
    with a passkey, and only within an hour. Inviting the same role and name
    again replaces the earlier invitation."""
    slug = payload.workspace.strip()
    if not wsvc.is_member(request.user, slug):
        raise HttpError(404, f"workspace '{slug}' not found")
    if not perms.can(request.user, slug, perms.AGENT_WORK):
        raise HttpError(403, "inviting a runner into a workspace needs permission to run work there")
    # A runner is owned by a person (apps/harness/api.py::pair_runner): an
    # agent's login inviting a box would make a runner nobody can manage.
    if getattr(request.user, "agent_identity", None) is not None:
        raise HttpError(403, "a runner must be owned by a person; invite it with your own token")
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
    with transaction.atomic():
        RunnerInvitation.objects.filter(role_jkt=jkt, name=payload.name, consumed_at__isnull=True).delete()
        invitation = RunnerInvitation.objects.create(
            name=payload.name, workspace_id=slug, created_by=request.user,
            role_arn=role_arn, role_jkt=jkt, ecdh_public_jwk=ecdh,
        )
    activity.emit(activity.RUNNER_INVITED, workspace=slug, actor=request.user, surface="runner",
                  ref=str(invitation.pk), meta={"name": invitation.name, "role_arn": role_arn})
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

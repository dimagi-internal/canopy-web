"""Runner fleets — the routes (`apps/harness/fleets.py` is the mechanism).

* `POST /api/harness/runner-fleets/` — `fleet.sh up`, with its creator's own
  token, once the fleet's stack exists: a PENDING fleet and the invitation its
  creator approves on the usual approve page, from a browser behind a passkey. A
  token cannot approve it (`api_runner_invitations.approve_runner_invitation`).
* `GET /api/harness/runner-fleets/` and `/{id}` — your fleets and their members.
* `PUT /api/harness/runner-fleets/{id}/size` — the bounds the scaler keeps it in.
* `POST /api/harness/runner-fleets/{id}/retire` — no more members join and the
  scaler leaves its group alone (`fleet.sh down` then sets the group to zero).

Only a fleet's owner sees or changes it, as only an invitation's creator does.
"""
from __future__ import annotations

import uuid

from django.http import HttpRequest
from ninja import Router, Status
from ninja.errors import HttpError

from apps.api.auth import session_auth
from apps.api.route_gates import gate
from apps.events import activity
from apps.events import audit as events_audit

from . import aws_iam_proof, device_key_notify, device_keys, fleets
from .api_runner_invitations import _may_invite_into, approve_url, fleet_out, invitation_out
from .models import RunnerCredentialSet, RunnerFleet
from .schemas import RunnerFleetIn, RunnerFleetOut, RunnerFleetSizeIn, RunnerInvitationOut

router = Router(auth=session_auth, tags=["harness"])


def _own_fleet_or_404(request: HttpRequest, fleet_id: uuid.UUID) -> RunnerFleet:
    fleet = (RunnerFleet.objects.select_related("owner", "credential_set")
             .filter(pk=fleet_id, owner=request.user).first())
    if fleet is None:  # someone else's is "not found", as an invitation is
        raise HttpError(404, "fleet not found")
    return fleet


@router.post("/", response={201: RunnerInvitationOut},
             summary="Ask for a runner fleet (approved by you, with a passkey)")
# member (else 404) with agent.work in the named workspace, as for a box's invitation
# asking for a fleet is asking for its invitation; approving it is the passkey-gated
# approve route (runner.invited / .approved activity)
@gate("agent.work", kind="request")
def create_runner_fleet(request: HttpRequest, payload: RunnerFleetIn):
    """A fleet of interchangeable cloud runners: the instances of one EC2 Auto
    Scaling group (`group_name`) signing with one instance role (`role_arn`),
    whose ECDH key is the fleet's KMS key (`ecdh_public_jwk`). Returns the
    invitation to approve at `approve_url`, once, with a passkey; after that every
    instance the group starts joins on its own as your runner in `workspace`, and
    canopy-web keeps the group between `min_members` and `max_members` by the work
    waiting for it. Asking again before approving replaces what you asked."""
    slug = _may_invite_into(request, payload.workspace)
    if payload.min_members > payload.max_members:
        raise HttpError(422, "min_members must not be more than max_members")
    cset = None
    if payload.credential_set is not None:
        # Someone else's set is "not found", as their fleet is.
        cset = RunnerCredentialSet.objects.filter(pk=payload.credential_set, owner=request.user).first()
        if cset is None:
            raise HttpError(404, "credential set not found")
    try:
        invitation = fleets.create(
            user=request.user, name=payload.name, workspace=slug, role_arn=payload.role_arn,
            ecdh_public_jwk=payload.ecdh_public_jwk, region=payload.region, group_name=payload.group_name,
            min_members=payload.min_members, max_members=payload.max_members,
            member_turns=payload.member_turns, credential_set=cset)
    except (fleets.FleetRefused, aws_iam_proof.AwsProofError, device_keys.KeyError_) as exc:
        raise HttpError(422, str(exc)) from exc
    activity.emit(activity.RUNNER_INVITED, workspace=slug, actor=request.user, surface="runner",
                  ref=str(invitation.pk), meta={"name": invitation.name, "fleet": payload.name})
    device_key_notify.notify_invitation_on_commit(invitation, approve_url(invitation))
    return Status(201, invitation_out(invitation))


@router.get("/", response=list[RunnerFleetOut], summary="Your runner fleets")
@gate("self")  # the fleets the caller owns
def list_runner_fleets(request: HttpRequest):
    """Every fleet you own, with its live members and where the scaler has each."""
    return [fleet_out(f) for f in RunnerFleet.objects.select_related("owner", "credential_set")
            .filter(owner=request.user)]


@router.get("/{fleet_id}", response=RunnerFleetOut, summary="A runner fleet and its members")
@gate("self")  # its owner only
def get_runner_fleet(request: HttpRequest, fleet_id: uuid.UUID):
    """The fleet, its bounds, its credential set and its live members."""
    return fleet_out(_own_fleet_or_404(request, fleet_id))


@router.put("/{fleet_id}/size", response=RunnerFleetOut, summary="Set the bounds a fleet scales between")
# its owner only
@gate("self", setting=True)
def set_runner_fleet_size(request: HttpRequest, fleet_id: uuid.UUID, payload: RunnerFleetSizeIn):
    """`min_members` stay in service however quiet it is (at least one: inbox
    polling and scheduled work need a box); the scaler never runs more than
    `max_members`. Takes effect on the scaler's next tick."""
    fleet = _own_fleet_or_404(request, fleet_id)
    if payload.min_members > payload.max_members:
        raise HttpError(422, "min_members must not be more than max_members")
    before = {"min_members": fleet.min_members, "max_members": fleet.max_members}
    after = {"min_members": payload.min_members, "max_members": payload.max_members}
    if before != after:
        fleet.min_members, fleet.max_members = payload.min_members, payload.max_members
        fleet.save(update_fields=["min_members", "max_members", "updated_at"])
    events_audit.note_change(scope=f"runner-fleet:{fleet.name}", before=before, after=after,
                             workspace=fleet.workspace)
    return fleet_out(fleet)


@router.post("/{fleet_id}/retire", response=RunnerFleetOut, summary="Retire a runner fleet")
# its owner only; ends the fleet (no more joins, no more scaling) — a lifecycle act,
# not a setting a person tunes
@gate("self", kind="action")
def retire_runner_fleet(request: HttpRequest, fleet_id: uuid.UUID):
    """No instance joins any more and canopy-web stops sizing its group. Members
    already running keep their work until their instances go (`fleet.sh down`
    sets the group to zero); each is retired then, and its chats move."""
    fleet = _own_fleet_or_404(request, fleet_id)
    fleets.retire(fleet)
    fleet.refresh_from_db()
    return fleet_out(fleet)

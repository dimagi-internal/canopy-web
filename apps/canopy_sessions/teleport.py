"""Teleport: ask to move a session onto another runner, decided by that runner's
administrator, then carried out by `services.transfer_session`.

`transfer_session` already does the hard part (re-point the binding, open a new
transcript epoch, hand the cold session a brief). What it never asked is whose
box the session lands on. With every runner belonging to one operator that didn't
matter; once a colleague runs an agent on their own laptop (2026-10-02) it does —
a transfer spends their machine and Claude subscription. So a move onto a box the
requester does not administer becomes a REQUEST, and the box's administrator
(`can_administer_runner`: its pairer, or someone they granted) approves it. A move
between two boxes of the SAME owner never asks.

There is ONE way in: `POST /{id}/transfer` calls `request()`, which either moves
now or opens the request. (There used to be a separate `/teleport` route that did
the same thing; two doors to one operation was removed on 2026-10-02.)

Notifications ride `teleport_changed` (fired after commit), so this framework
module never imports Slack or push; their receivers post to the session's Slack
thread and ping the approvers.
"""
from __future__ import annotations

import datetime as dt
import uuid

from django.db import IntegrityError, transaction
from django.db.models import Q
from django.dispatch import Signal
from django.utils import timezone

from apps.harness import services as harness_services
from apps.harness.models import Runner, RunnerAdmin

from . import services
from .models import Session, TeleportRequest

#: Fired after commit with `request=<TeleportRequest>` whenever one is created or
#: decided. Receivers must never raise into the caller.
teleport_changed = Signal()

#: A request nobody answered in this long is dead: the session has moved on, and
#: approving it a day later would yank a conversation out from under whoever is in it.
TTL = dt.timedelta(hours=24)


def resolve_runner(value: str) -> Runner | None:
    """A runner by id or, for a person typing into an MCP client, by name."""
    value = (value or "").strip()
    qs = Runner.objects.exclude(status=Runner.RETIRED)
    try:
        return qs.filter(pk=uuid.UUID(value)).first()
    except ValueError:
        return qs.filter(name=value).first()


def approvers(runner: Runner) -> list:
    """Everyone who may say yes for this box: its pairer and its admin grantees."""
    users = [runner.paired_by] if runner.paired_by_id else []
    users += [a.user for a in RunnerAdmin.objects.filter(runner=runner).select_related("user")]
    return list({u.pk: u for u in users}.values())


def _fire(req: TeleportRequest) -> None:
    transaction.on_commit(lambda: teleport_changed.send(sender=TeleportRequest, request=req))


def _expire_if_stale(req: TeleportRequest) -> bool:
    if req.status == TeleportRequest.PENDING and timezone.now() - req.created_at > TTL:
        req.status, req.decided_at, req.note = TeleportRequest.EXPIRED, timezone.now(), "expired unanswered"
        req.save(update_fields=["status", "decided_at", "note"])
        return True
    return False


def _transfer(req: TeleportRequest, user, initiator):
    """Carry it out. RuntimeError (a turn is executing) propagates — the caller
    keeps the request pending and says to stop the session first."""
    return services.transfer_session(
        session=req.session, placement=str(req.to_runner_id), brief=req.brief, user=user,
        initiator=initiator,
    )


def request(*, session: Session, runner_value: str, brief: str, user, initiator=None):
    """Ask to move `session` onto `runner_value`. Returns (request, transfer|None).

    A requester who administers the target is not asking anyone: the move runs now
    and the row is written APPROVED. Everyone else gets a PENDING row and the
    target's administrators are told. Raises ValueError for a target the session
    could never run on (the same `_placeable_runner` gate a transfer applies, so a
    request can't wait a day for an approval that would then fail), LookupError for
    a session with nothing on a box, RuntimeError while a turn executes on the
    immediate path, and FileExistsError when a request is already open.
    """
    target = resolve_runner(runner_value)
    if target is None or services._placeable_runner(session, str(target.id)) is None:
        raise services._placement_refused(
            session, str(target.id) if target else runner_value, "unknown runner for teleport")
    if session.status != Session.ACTIVE:
        raise ValueError("cannot teleport an archived session")
    binding = getattr(session, "runner_binding", None)
    if binding is None:
        raise LookupError("session has no runner binding to move")
    if binding.runner_id == target.id:
        raise ValueError(f"session is already on '{target.name}'")
    for stale in TeleportRequest.objects.filter(session=session, status=TeleportRequest.PENDING):
        _expire_if_stale(stale)

    # No one to ask when the requester already administers the target, or when the
    # move stays inside one owner's boxes (two macOS accounts of the same person —
    # ada's user-switch): the machine and subscription spent are the same owner's.
    same_owner = (binding.runner_id is not None
                  and binding.runner.paired_by_id is not None
                  and binding.runner.paired_by_id == target.paired_by_id)
    if same_owner or harness_services.can_administer_runner(user, target):
        binding_after, turn = services.transfer_session(
            session=session, placement=str(target.id), brief=brief, user=user,
            initiator=initiator)
        req = TeleportRequest.objects.create(
            session=session, to_runner=target, from_runner=binding.runner, requested_by=user,
            brief=brief, status=TeleportRequest.APPROVED, decided_by=user,
            decided_at=timezone.now(), turn_id=turn.id,
            note=("same owner on both runners — no approval needed" if same_owner
                  else "requester administers the target — no approval needed"),
        )
        _fire(req)
        return req, (binding_after, turn)

    try:
        with transaction.atomic():
            req = TeleportRequest.objects.create(
                session=session, to_runner=target, from_runner=binding.runner,
                requested_by=user, brief=brief,
            )
    except IntegrityError:
        raise FileExistsError("this session already has a teleport request waiting — "
                              "cancel it or wait for an answer")
    _fire(req)
    return req, None


def visible_to(user):
    """Requests this person can see: ones they asked for, and ones waiting on a
    box they administer."""
    administered = Q(to_runner__paired_by=user) | Q(to_runner__admins__user=user)
    return (TeleportRequest.objects.filter(Q(requested_by=user) | administered)
            .select_related("session", "session__agent", "to_runner", "from_runner",
                            "requested_by", "decided_by")
            .distinct())


def approve(*, req: TeleportRequest, user, initiator=None):
    """Approve and carry out. Admin is re-checked HERE, at decision time — a grant
    revoked since the request was made must not still be honoured."""
    if not harness_services.can_administer_runner(user, req.to_runner):
        raise PermissionError(f"you don't administer runner '{req.to_runner.name}'")
    if _expire_if_stale(req):
        raise ValueError("this request expired unanswered — ask again")
    if req.status != TeleportRequest.PENDING:
        raise ValueError(f"this request is already {req.status}")
    binding, turn = _transfer(req, user, initiator)
    req.status, req.decided_by, req.decided_at, req.turn_id = (
        TeleportRequest.APPROVED, user, timezone.now(), turn.id)
    req.save(update_fields=["status", "decided_by", "decided_at", "turn_id"])
    _fire(req)
    return binding, turn


def decline(*, req: TeleportRequest, user, note: str = ""):
    if not harness_services.can_administer_runner(user, req.to_runner):
        raise PermissionError(f"you don't administer runner '{req.to_runner.name}'")
    return _close(req, user, TeleportRequest.DECLINED, note)


def cancel(*, req: TeleportRequest, user):
    if req.requested_by_id != getattr(user, "id", None):
        raise PermissionError("only the person who asked can cancel a teleport request")
    return _close(req, user, TeleportRequest.CANCELLED, "")


def _close(req: TeleportRequest, user, status: str, note: str):
    if _expire_if_stale(req):
        raise ValueError("this request already expired")
    if req.status != TeleportRequest.PENDING:
        raise ValueError(f"this request is already {req.status}")
    req.status, req.decided_by, req.decided_at, req.note = status, user, timezone.now(), note[:300]
    req.save(update_fields=["status", "decided_by", "decided_at", "note"])
    _fire(req)
    return req

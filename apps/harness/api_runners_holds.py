"""/api/harness routes: the fleet hold and stop, and a workspace's hold and stop.
"""
from __future__ import annotations

from django.conf import settings
from django.db import transaction
from django.http import HttpRequest
from django.utils import timezone
from ninja.errors import HttpError

from apps.api.route_gates import gate
from apps.canopy_sessions import control
from apps.workspaces import permissions as perms
from apps.workspaces import services as wsvc
from apps.workspaces.models import Workspace

from . import (
    routing,
)
from .api import (  # noqa: E402
    router,
)
from .models import (
    FleetHold,
    Turn,
)
from .schemas import (
    FleetHoldIn,
    FleetHoldOut,
    FleetStopOut,
    WorkspaceHoldOut,
)


def _may_release_fleet_hold(user) -> bool:
    return bool(getattr(user, "is_superuser", False))


def _may_hold_fleet(user) -> bool:
    """A superuser, or a named holder (`CANOPY_FLEET_HOLDERS` — Ada). Holding is the
    safe direction: it starts nothing and loses nothing, so the fleet's conductor may
    pull it unattended. Releasing is not widened (see `_may_release_fleet_hold`)."""
    if _may_release_fleet_hold(user):
        return True
    email = (getattr(user, "email", "") or "").strip().lower()
    holders = {e.strip().lower() for e in getattr(settings, "CANOPY_FLEET_HOLDERS", []) if e.strip()}
    return bool(email) and email in holders


def _fleet_hold_out(hold: FleetHold, user) -> dict:
    return {
        "can_hold": _may_hold_fleet(user),
        "can_release": _may_release_fleet_hold(user),
        "can_stop_running": _may_release_fleet_hold(user),
        "held": hold.held, "note": hold.note, "held_at": hold.held_at,
        "held_by_email": (hold.held_by.email if hold.held_by_id else ""),
        "queued": Turn.objects.filter(status=Turn.QUEUED).count() if hold.held else 0,
    }


@router.get("/fleet-hold", response=FleetHoldOut)
@gate("authenticated")
def get_fleet_hold(request: HttpRequest):
    """Is the whole fleet on hold? Readable by anyone signed in — a member whose turn
    is sitting queued should be able to see why."""
    return _fleet_hold_out(FleetHold.current(), request.user)


@router.post("/fleet-hold", response=FleetHoldOut)
@gate("superuser", "fleet-holder", setting=True)
def hold_fleet(request: HttpRequest, payload: FleetHoldIn):
    """Stop EVERY runner from starting anything — the fleet-wide sibling of
    /runners/{id}/pause (see FleetHold). Turns keep enqueuing and wait QUEUED with
    their trigger, so `list turns?status=queued` shows what tried to start while
    held. Running turns finish normally — stopping them is /fleet-stop. A superuser
    or a named holder (`CANOPY_FLEET_HOLDERS`, i.e. Ada) — it spans every tenant,
    but only stops work. Idempotent — holding again refreshes the note."""
    if not _may_hold_fleet(request.user):
        raise HttpError(403, "holding the whole fleet requires a superuser or a named fleet holder")
    hold = FleetHold.current()
    if not hold.held:
        hold.held_at = timezone.now()
        hold.held_by = request.user
    hold.held = True
    hold.note = (payload.note or "")[:500]
    hold.save()
    return _fleet_hold_out(hold, request.user)


@router.post("/fleet-stop", response=FleetStopOut)
@gate("superuser", setting=True)
def stop_fleet(request: HttpRequest):
    """Stop EVERYTHING running, in every workspace, now: every session working
    right now is stopped and every executing turn cancelled
    (`control.stop_everything`). A one-shot action, separate from the hold: it does
    not keep new work from starting — hold the fleet for that. Queued turns are
    left alone, and nothing is archived. Superuser only."""
    if not _may_release_fleet_hold(request.user):
        raise HttpError(403, "stopping everything running across the fleet requires a superuser")
    stopped = control.stop_everything(by=control.person_name(request.user))
    return {"stopped_sessions": stopped["sessions"], "stopped_turns": stopped["turns"]}


# --- the workspace controls: the same two switches, for one workspace's tree ---------


def _workspace_or_404(request: HttpRequest, slug: str) -> Workspace:
    """The workspace, if the caller is in it — a stranger gets the same 404 as a
    missing one, so this never confirms a workspace exists."""
    ws = Workspace.objects.filter(slug=slug).first()
    if ws is None or not wsvc.is_member(request.user, slug):
        raise HttpError(404, f"workspace '{slug}' not found")
    return ws


def _require_control(request: HttpRequest, ws: Workspace) -> None:
    if not perms.can(request.user, ws, perms.CONTROL):
        raise HttpError(403, "holding or stopping a workspace needs its owner or a superuser")


def _workspace_tree(ws: Workspace) -> set[str]:
    return {ws.slug} | wsvc.descendant_map({ws.slug})[ws.slug]


def _workspace_hold_out(request: HttpRequest, ws: Workspace) -> dict:
    holder = wsvc.held_by(ws.slug)
    queued = 0
    if holder is not None:
        queued = Turn.objects.filter(status=Turn.QUEUED).filter(
            routing.turn_workspace_q(_workspace_tree(ws))).count()
    return {
        "workspace": ws.slug,
        "held": holder is not None,
        "held_by_workspace": holder.slug if holder is not None and holder.slug != ws.slug else "",
        "note": holder.held_note if holder is not None else "",
        "held_at": holder.held_at if holder is not None else None,
        "held_by_email": holder.held_by.email if holder is not None and holder.held_by_id else "",
        "queued": queued,
        "can_control": perms.can(request.user, ws, perms.CONTROL),
    }


@router.get("/workspaces/{slug}/hold", response=WorkspaceHoldOut)
@gate("member")
def get_workspace_hold(request: HttpRequest, slug: str):
    """Is this workspace on hold — by its own hold or an ancestor's? Any member may
    read it: a member whose turn sits queued should be able to see why."""
    return _workspace_hold_out(request, _workspace_or_404(request, slug))


@router.post("/workspaces/{slug}/hold", response=WorkspaceHoldOut)
@gate("control", setting=True)
def hold_workspace(request: HttpRequest, slug: str, payload: FleetHoldIn):
    """Stop every runner from starting anything in this workspace or any below it
    (`Workspace.held`, enforced in the claim). Turns keep enqueuing and wait QUEUED;
    running work finishes — stopping it is /stop. An owner or a superuser of the
    workspace. Idempotent — holding again refreshes the note."""
    ws = _workspace_or_404(request, slug)
    _require_control(request, ws)
    if not ws.held:
        ws.held_at = timezone.now()
        ws.held_by = request.user
    ws.held = True
    ws.held_note = (payload.note or "")[:500]
    ws.save(update_fields=["held", "held_note", "held_at", "held_by", "updated_at"])
    return _workspace_hold_out(request, ws)


@router.post("/workspaces/{slug}/hold/release", response=WorkspaceHoldOut)
@gate("control", setting=True)
def release_workspace_hold(request: HttpRequest, slug: str):
    """Release this workspace's own hold; its queued turns become claimable again
    (unless an ancestor's hold still covers it — release that one there). An owner or
    a superuser of the workspace."""
    ws = _workspace_or_404(request, slug)
    _require_control(request, ws)
    if ws.held:
        ws.held, ws.held_note, ws.held_at, ws.held_by = False, "", None, None
        ws.save(update_fields=["held", "held_note", "held_at", "held_by", "updated_at"])
        from apps.realtime import groups  # noqa: PLC0415 — lazy, as in release_fleet_hold below

        slugs = _workspace_tree(ws)
        transaction.on_commit(lambda: [
            groups.publish(groups.runnable_group(s), {"type": "runner.wake"}) for s in slugs
        ])
    return _workspace_hold_out(request, ws)


@router.post("/workspaces/{slug}/stop", response=FleetStopOut)
@gate("control", setting=True)
def stop_workspace(request: HttpRequest, slug: str):
    """Stop everything running in this workspace and every one below it, now: each
    working session is stopped and each executing turn cancelled. A one-shot action,
    separate from the hold. Queued turns are left alone; nothing is archived. An
    owner or a superuser of the workspace."""
    ws = _workspace_or_404(request, slug)
    _require_control(request, ws)
    stopped = control.stop_everything(by=control.person_name(request.user),
                                      workspaces=_workspace_tree(ws))
    return {"stopped_sessions": stopped["sessions"], "stopped_turns": stopped["turns"]}


@router.post("/fleet-hold/release", response=FleetHoldOut)
@gate("superuser", setting=True)
def release_fleet_hold(request: HttpRequest):
    """Release the fleet hold; queued turns become claimable again. Wakes every
    tenant's runners so the backlog starts now rather than at each box's next poll.
    Superuser only — a named holder may stop the fleet, never restart it."""
    if not _may_release_fleet_hold(request.user):
        raise HttpError(403, "releasing the fleet hold requires a superuser")
    hold = FleetHold.current()
    if hold.held:
        hold.held, hold.note, hold.held_at, hold.held_by = False, "", None, None
        hold.save()
        from apps.realtime import groups

        slugs = list(Workspace.objects.values_list("slug", flat=True))
        transaction.on_commit(lambda: [
            groups.publish(groups.runnable_group(slug), {"type": "runner.wake"})
            for slug in slugs
        ])
    return _fleet_hold_out(hold, request.user)

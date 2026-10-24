"""Django Ninja router for /api/harness/failure-investigations — what the fleet's
debugger agent reads and closes.

`apps/harness/auto_debug.py` opens these when a turn fails and starts a debug
turn whose prompt names one. The debugger (ada) needs to see them and end each
one with exactly one verb: resolve (fixed — closes its alert, silently, or
leaves it quietly pending deploy) or hand off (needs a person — reassigns its
alert to them, the one notification). Every route is an MCP tool by generation
(operationId = the function name), so `list_failure_investigations`,
`get_failure_investigation`, `resolve_failure_investigation` and
`hand_off_failure_investigation` exist with no further step.

Who: the workspace's log readers (admins and up — it is turn content), the
debugger agent's admins, and the debugger's own canopy login
(`auto_debug.can_handle`). Anyone else gets the uniform 404.
"""
from __future__ import annotations

from django.contrib.auth import get_user_model
from django.http import HttpRequest
from ninja import Router
from ninja.errors import HttpError

from apps.api.auth import session_auth
from apps.api.pagination import Page, clamp_limit, clamp_offset, paginate
from apps.api.route_gates import gate
from apps.workspaces import services as wsvc

from . import auto_debug
from .models import FailureInvestigation
from .schemas import (
    FailureInvestigationOut,
    HandOffInvestigationIn,
    InvestigationStatus,
    ResolveInvestigationIn,
)

router = Router(auth=session_auth, tags=["harness"])


def _visible_slugs(request: HttpRequest, *, listing: bool = False) -> set[str]:
    """Where the caller may handle investigations. A LIST takes the read rule
    (`wsvc.request_workspace_slugs`: a caller in two tenant trees must pin
    one); a by-id gate takes every membership."""
    scope = wsvc.request_workspace_slugs(request) if listing else wsvc.request_member_slugs(request)
    return {s for s in scope if auto_debug.can_handle(request.user, s)}


def _investigation_or_404(request: HttpRequest, investigation_id: int) -> FailureInvestigation:
    inv = FailureInvestigation.objects.filter(pk=investigation_id).first()
    if inv is None or inv.workspace_id not in _visible_slugs(request):
        raise HttpError(404, "investigation not found")
    return inv


@router.get("/", response=Page[FailureInvestigationOut],
            summary="List failure investigations")
@gate("logs.read", "agent-admin")  # log readers, the debugger agent's admins, or the debugger's own login (Agent.user)
def list_failure_investigations(request: HttpRequest, status: InvestigationStatus | None = None,
                                limit: int = 50, offset: int = 0):
    """Turn failures canopy grouped by fingerprint and handed to the debugger
    agent, newest first. `status`: open (a debug turn has it), held (the rate cap
    stopped its turn; it rides along in the next one), debugger_failed (its debug
    turn failed), pending_deploy (fixed by a commit some runner it hit does not
    run yet), resolved, escalated (with a person)."""
    qs = FailureInvestigation.objects.filter(workspace_id__in=_visible_slugs(request, listing=True))
    if status:
        qs = qs.filter(status=status)
    return paginate(qs.order_by("-last_seen", "-id"), offset=clamp_offset(offset), limit=clamp_limit(limit))


@router.get("/{investigation_id}", response=FailureInvestigationOut,
            summary="Get one failure investigation")
@gate("logs.read", "agent-admin")
def get_failure_investigation(request: HttpRequest, investigation_id: int):
    """One investigation: the normalized failure, its latest raw note, the recent
    failed turns, the agents and runners it hit, and its debug turn."""
    return _investigation_or_404(request, investigation_id)


@router.post("/{investigation_id}/resolve", response=FailureInvestigationOut,
             summary="Resolve a failure investigation")
@gate("logs.read", "agent-admin", kind="content")
def resolve_failure_investigation(request: HttpRequest, investigation_id: int,
                                  payload: ResolveInvestigationIn):
    """Mark the failure fixed, saying what was wrong and what changed. That closes
    its alert and notifies nobody. Give `fix_commit` (and `fix_committed_at`) when
    the fix is to the runner code that failed: while a runner it hit does not run
    that commit yet, the alert stays open quietly as pending deploy and closes on
    its own once the fix is live everywhere it failed. 409 when it is with a
    person (handed off) and you are the debugger: only a person closes it then."""
    inv = _investigation_or_404(request, investigation_id)
    try:
        return auto_debug.resolve(inv, note=payload.note, by=request.user,
                                  fix_commit=payload.fix_commit,
                                  fix_committed_at=payload.fix_committed_at,
                                  pr=payload.pr, merged=payload.merged)
    except ValueError as exc:
        raise HttpError(409, str(exc)) from exc


@router.post("/{investigation_id}/hand-off", response=FailureInvestigationOut,
             summary="Hand a failure investigation to a person")
@gate("logs.read", "agent-admin", kind="content")
def hand_off_failure_investigation(request: HttpRequest, investigation_id: int,
                                   payload: HandOffInvestigationIn):
    """It needs a person: reassign its alert to them with a one-line `ask`,
    keeping it open. They are notified once; further repeats only count. `to` is
    their email (default: the debugger agent's owner) and must be someone who
    may handle investigations in its workspace. 409 when it is already resolved."""
    inv = _investigation_or_404(request, investigation_id)
    person = None
    if payload.to:
        person = (get_user_model().objects
                  .filter(email__iexact=payload.to.strip(), is_active=True).first())
        if person is None or not auto_debug.can_handle(person, inv.workspace_id):
            raise HttpError(400, f"{payload.to} cannot handle investigations in {inv.workspace_id}")
    try:
        return auto_debug.hand_off(inv, ask=payload.ask, to=person, by=request.user)
    except ValueError as exc:
        raise HttpError(409, str(exc)) from exc

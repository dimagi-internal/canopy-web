"""Django Ninja router for /api/harness/failure-investigations — what the fleet's
debugger agent reads and closes.

`apps/harness/auto_debug.py` opens these when a turn fails and starts a debug
turn whose prompt names one. The debugger (ada) needs two verbs back: see them,
and say one is fixed — resolving is what arms the "the fix did not hold —
escalate" path if the failure comes back. Every route is an MCP tool by
generation (operationId = the function name), so `list_failure_investigations`,
`get_failure_investigation` and `resolve_failure_investigation` exist with no
further step.

Who: the workspace's log readers (admins and up — it is turn content), the
debugger agent's admins, and the debugger's own canopy login
(`auto_debug.can_handle`). Anyone else gets the uniform 404.
"""
from __future__ import annotations

from django.http import HttpRequest
from ninja import Router
from ninja.errors import HttpError

from apps.api.auth import session_auth
from apps.api.pagination import Page, clamp_limit, paginate
from apps.workspaces import services as wsvc

from . import auto_debug
from .models import FailureInvestigation
from .schemas import FailureInvestigationOut, InvestigationStatus, ResolveInvestigationIn

router = Router(auth=session_auth, tags=["harness"])


def _visible_slugs(request: HttpRequest) -> set[str]:
    return {s for s in wsvc.request_workspace_slugs(request)
            if auto_debug.can_handle(request.user, s)}


def _investigation_or_404(request: HttpRequest, investigation_id: int) -> FailureInvestigation:
    inv = FailureInvestigation.objects.filter(pk=investigation_id).first()
    if inv is None or inv.workspace_id not in _visible_slugs(request):
        raise HttpError(404, "investigation not found")
    return inv


@router.get("/", response=Page[FailureInvestigationOut],
            summary="List failure investigations")
def list_failure_investigations(request: HttpRequest, status: InvestigationStatus | None = None,
                                limit: int = 50, offset: int = 0):
    """Turn failures canopy grouped by fingerprint and handed to the debugger
    agent, newest first. `status`: open (a debug turn has it), held (the rate cap
    stopped its turn; it rides along in the next one), debugger_failed (its debug
    turn failed), resolved, escalated (it came back after a fix)."""
    qs = FailureInvestigation.objects.filter(workspace_id__in=_visible_slugs(request))
    if status:
        qs = qs.filter(status=status)
    return paginate(qs.order_by("-last_seen", "-id"), offset=max(0, offset), limit=clamp_limit(limit))


@router.get("/{investigation_id}", response=FailureInvestigationOut,
            summary="Get one failure investigation")
def get_failure_investigation(request: HttpRequest, investigation_id: int):
    """One investigation: the normalized failure, its latest raw note, the recent
    failed turns, the agents and runners it hit, and its debug turn."""
    return _investigation_or_404(request, investigation_id)


@router.post("/{investigation_id}/resolve", response=FailureInvestigationOut,
             summary="Resolve a failure investigation")
def resolve_failure_investigation(request: HttpRequest, investigation_id: int,
                                  payload: ResolveInvestigationIn):
    """Mark the failure fixed, saying what was changed. If the same failure comes
    back afterwards, canopy sends it to the debugger once more framed as an
    escalation (the fix did not hold), then only counts further repeats."""
    inv = _investigation_or_404(request, investigation_id)
    return auto_debug.resolve(inv, note=payload.note, by=request.user)

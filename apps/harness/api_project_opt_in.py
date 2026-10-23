"""`/api/me/canopy-projects/` — which of your repos canopy works in.

Part of your canopy account, not config on a box (`ProjectOptIn`): every runner
you own reads the same answer. Unset is nothing — canopy is opt-in.
docs/architecture/runners.md § Opted-in projects.
"""
from __future__ import annotations

from django.http import HttpRequest
from ninja import Router

from apps.api.auth import session_auth
from apps.api.route_gates import gate
from apps.events import audit

from .models import ProjectOptIn, Runner
from .schemas import ProjectOptInIn, ProjectOptInOut

router = Router(auth=session_auth, tags=["harness"])


def _out(user, row: ProjectOptIn) -> ProjectOptInOut:
    held: set[str] = set()
    for runner in Runner.objects.filter(owner=user).exclude(status=Runner.RETIRED).only("capabilities"):
        held.update(runner.observed_projects())
    return ProjectOptInOut(everything=row.everything, projects=list(row.projects or []),
                           available=sorted(held | set(row.projects or [])))


@router.get("/", response=ProjectOptInOut, summary="Get the repos canopy works in for you")
@gate("self")
def get_project_opt_in(request: HttpRequest) -> ProjectOptInOut:
    """Whether canopy works in every repo on your boxes, or only the ones you
    named. A repo outside it is plain Claude Code: canopy routes no repo turn
    to it, mirrors no session or transcript from it, and reads no usage from it.
    An agent's own repo is always canopy's. Unset: nothing."""
    return _out(request.user, ProjectOptIn.for_user_id(request.user.pk))


@router.put("/", response=ProjectOptInOut, summary="Set the repos canopy works in for you")
@gate("self", setting=True)
def set_project_opt_in(request: HttpRequest, payload: ProjectOptInIn) -> ProjectOptInOut:
    """Replace your choice: `everything`, or exactly `projects`. Takes effect on
    every runner you own at once. Sessions already mirrored from a repo you
    remove stay in canopy, but nothing more of them is sent."""
    before = ProjectOptIn.for_user_id(request.user.pk)
    old = {"everything": before.everything, "projects": list(before.projects or [])}
    new = {"everything": payload.everything, "projects": ProjectOptIn.clean_names(payload.projects)}
    row, _ = ProjectOptIn.objects.update_or_create(user=request.user, defaults=new)
    audit.note_change(scope="canopy-projects", before=old, after=new)
    return _out(request.user, row)

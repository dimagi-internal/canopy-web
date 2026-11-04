"""`/api/me/canopy-projects/` — which of your repos canopy works in.

Part of your canopy account, not config on a box (`ProjectOptIn`): every runner
you own reads the same answer. Unset is nothing — canopy is opt-in.
docs/architecture/runners.md § Opted-in projects.
"""
from __future__ import annotations

from django.http import HttpRequest
from ninja import Router
from ninja.errors import HttpError

from apps.api.auth import session_auth
from apps.api.route_gates import gate
from apps.events import audit
from apps.workspaces import permissions as perms
from apps.workspaces.models import Workspace

from .models import ProjectOptIn, Runner
from .schemas import ProjectOptInIn, ProjectOptInOut, ProjectOptInWorkspaceOut

router = Router(auth=session_auth, tags=["harness"])


def _out(user, row: ProjectOptIn) -> ProjectOptInOut:
    held: set[str] = set()
    defaults: dict[str, set[str]] = {}
    for runner in Runner.objects.filter(owner=user).exclude(status=Runner.RETIRED).only("capabilities", "workspace"):
        for repo in runner.observed_projects():
            held.add(repo)
            if runner.workspace_id:
                defaults.setdefault(repo, set()).add(runner.workspace_id)
    assignable = perms.slugs_with(user, perms.AGENT_WORK)
    return ProjectOptInOut(
        everything=row.everything, projects=list(row.projects or []),
        available=sorted(held | set(row.projects or [])),
        repo_workspaces=dict(row.workspaces or {}),
        default_workspaces={repo: sorted(slugs) for repo, slugs in defaults.items()},
        workspaces=[ProjectOptInWorkspaceOut(slug=w.slug, display_name=w.display_name)
                    for w in Workspace.objects.filter(pk__in=assignable).order_by("slug")],
    )


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
    """Replace your choice: `everything`, or exactly `projects`, and when given,
    every repo's workspaces. Takes effect on every runner you own at once. Sessions
    already mirrored from a repo you remove stay in canopy, but nothing more of them
    is sent. A chat already running on a box stays there."""
    before = ProjectOptIn.for_user_id(request.user.pk)
    old = {"everything": before.everything, "projects": list(before.projects or []),
           "workspaces": dict(before.workspaces or {})}
    assigned = (dict(before.workspaces or {}) if payload.repo_workspaces is None
                else ProjectOptIn.clean_workspaces(payload.repo_workspaces))
    if payload.repo_workspaces is not None:
        # Compared with what you could already assign: a workspace you have since
        # lost must not stop you saving, and one you never had is refused by name.
        may = perms.slugs_with(request.user, perms.AGENT_WORK) | {
            s for slugs in (before.workspaces or {}).values() for s in slugs}
        for slugs in assigned.values():
            for slug in slugs:
                if slug not in may:
                    raise HttpError(422, f"you cannot run work in workspace '{slug}'")
    new = {"everything": payload.everything, "projects": ProjectOptIn.clean_names(payload.projects),
           "workspaces": assigned}
    row, _ = ProjectOptIn.objects.update_or_create(user=request.user, defaults=new)
    audit.note_change(scope="canopy-projects", before=old, after=new)
    return _out(request.user, row)

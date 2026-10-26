"""`/api/me/session-sharing/` — share your laptop sessions in a project's repo
with that project's team (`ProjectSessionShare`, `sharing.py`).

Your own choice, per project, revocable at once; every change is an audited
setting change. docs/architecture/access.md § Who can see a session.
"""
from __future__ import annotations

from django.http import HttpRequest
from ninja import Router
from ninja.errors import HttpError

from apps.api.auth import session_auth
from apps.api.route_gates import gate
from apps.events import audit

from . import sharing
from .schemas import ProjectSessionShareIn, ProjectSessionShareOut

router = Router(auth=session_auth, tags=["chat"])


def _out(user, projects) -> list[dict]:
    shared = set(sharing.live(user).values_list("project_id", flat=True))
    return [{"project_id": p.pk, "handle": p.handle, "name": p.name, "workspace": p.workspace_id,
             "repo": p.repo_slug, "shared": p.pk in shared, "team_size": sharing.team_size(p)}
            for p in projects]


@router.get("/", response=list[ProjectSessionShareOut],
            summary="The projects you could share your laptop sessions with")
@gate("self")
def list_session_sharing(request: HttpRequest):
    """Every project in your workspaces whose repo is one you work in through
    canopy, and whether your laptop sessions in that repo are shared with its
    team. Unset: nothing is shared — a laptop session is yours alone."""
    return _out(request.user, sharing.candidates(request.user))


@router.put("/{project_id}", response=ProjectSessionShareOut,
            summary="Share (or stop sharing) your laptop sessions with a project's team")
@gate("self", setting=True)
def set_session_sharing(request: HttpRequest, project_id: int, payload: ProjectSessionShareIn):
    """On: every session discovered on a box you own, in this project's repo and
    workspace, is visible to the project's team — its owner and the people taking
    part in it, never an agent's own login — who read, write and may ask your
    machine for its live stream and history. Never a sensitive one. Off: the next
    read drops it."""
    # Only a project in one of your workspaces, in a repo you work in.
    project = sharing.candidates(request.user).filter(pk=project_id).first()
    if project is None:
        raise HttpError(404, "project not found")
    before = sharing.live(request.user).filter(project=project).exists()
    sharing.set_shared(request.user, project, payload.shared)
    audit.note_change(scope="session-sharing", workspace=project.workspace,
                      before={"project": project.handle, "shared": before},
                      after={"project": project.handle, "shared": payload.shared})
    [out] = _out(request.user, [project])
    return out

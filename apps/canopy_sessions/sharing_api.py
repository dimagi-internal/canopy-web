"""`/api/me/session-sharing/` — your live shares of laptop sessions with a
project's team (`ProjectSessionShare`, `sharing.py`), and turning one off.

New shares are refused (409): the feature is retired, and the ones that exist
keep working until their person turns them off. Every change is an audited
setting change. docs/architecture/access-sessions-and-people.md § Who can see a session.
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


def _out(project, shared: bool) -> dict:
    return {"project_id": project.pk, "handle": project.handle, "name": project.name,
            "workspace": project.workspace_id, "repo": project.repo_slug, "shared": shared,
            "team_size": sharing.team_size(project)}


@router.get("/", response=list[ProjectSessionShareOut],
            summary="The projects you still share your laptop sessions with")
@gate("self")
def list_session_sharing(request: HttpRequest):
    """Every project whose team can still read, write and pull your laptop
    sessions in its repo, because you shared them before this was retired.
    Empty for almost everyone: a laptop session is yours alone."""
    return [_out(p, True) for p in sharing.shared_projects(request.user)]


@router.put("/{project_id}", response=ProjectSessionShareOut,
            summary="Stop sharing your laptop sessions with a project's team")
@gate("self", setting=True)
def set_session_sharing(request: HttpRequest, project_id: int, payload: ProjectSessionShareIn):
    """`shared: false` turns off a share you have: the project's team stops
    seeing your laptop sessions in its repo on the next read. `shared: true` is
    refused (409): sharing all your sessions in a repo with a project's team is
    retired. To share one session with named people, add them on its page."""
    if payload.shared:
        raise HttpError(409, "Sharing your laptop sessions with a project team is retired. "
                             "To share one session, add people to it from its page.")
    project = sharing.shared_projects(request.user).filter(pk=project_id).first()
    if project is None:
        raise HttpError(404, "you do not share your sessions with this project")
    sharing.stop_sharing(request.user, project)
    audit.note_change(scope="session-sharing", workspace=project.workspace,
                      before={"project": project.handle, "shared": True},
                      after={"project": project.handle, "shared": False})
    return _out(project, False)

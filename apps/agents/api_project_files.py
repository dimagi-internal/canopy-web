"""A project's files over REST: read them, draft a change, publish, discard, roll back.

The rules are `project_files` (module docstring): anyone who may change the project
drafts; only a person who may change who is on it (`project_members.may_manage`),
acting as themselves, publishes or rolls back, because what is published runs in its
member agents' turns with the project's vault. Doc: `docs/architecture/project-files.md`.
"""
from __future__ import annotations

from django.http import HttpRequest
from ninja import Router
from ninja.errors import HttpError

from apps.api.auth import session_auth
from apps.api.errors import TYPE_CONFLICT, TYPE_FORBIDDEN, TYPE_NOT_FOUND, TYPE_VALIDATION, ProblemError, refusals
from apps.api.route_gates import gate
from apps.events import audit
from apps.harness import acting_person
from apps.passkeys import stepup

from . import project_files as pf
from . import project_members
from .gates import may_edit_project, project_ws_or_404
from .models import ProjectFilesRevision
from .schemas import (
    ProjectFileOut,
    ProjectFilesDraftIn,
    ProjectFilesOut,
    ProjectFilesPublishIn,
    ProjectFilesRevisionOut,
    ProjectFilesRevisionSummaryOut,
    ProjectFilesRollbackIn,
)

router = Router(auth=session_auth, tags=["projects"])

_TYPES = {404: TYPE_NOT_FOUND, 403: TYPE_FORBIDDEN, 409: TYPE_CONFLICT, 422: TYPE_VALIDATION}
_BASE = "/workspace-projects/{workspace}/{handle}/files"


def _problem(exc: pf.Refused) -> ProblemError:
    members = {"error": exc.code} if exc.code else {}
    if exc.problems:
        members["problems"] = exc.problems
    return ProblemError(exc.status, str(exc), type_=_TYPES.get(exc.status, TYPE_CONFLICT), members=members)


def _may_publish(request: HttpRequest, project) -> bool:
    return acting_person.refusal(request) is None and project_members.may_manage(project, request.user)


def _publisher(request: HttpRequest, workspace: str, handle: str):
    """The project, when the caller is a person who may publish its files; else 403."""
    project = project_ws_or_404(request, workspace, handle)
    why = acting_person.refusal(request)
    if why is not None:
        raise ProblemError(403, "publishing a project's files is a person's own act, never an agent's login "
                           "or a turn's token: what is published runs with the project's vault",
                           type_=TYPE_FORBIDDEN, members={"error": "not_a_publisher", "reason": why})
    if not project_members.may_manage(project, request.user):
        raise ProblemError(403, "publishing a project's files takes its lead, an admin on it, or an owner of its "
                           "workspace", type_=TYPE_FORBIDDEN, members={"error": "not_a_publisher",
                                                                        "reason": "not_project_admin"})
    return project


def _who(user) -> str:
    return getattr(user, "email", "") or ""


def _revision_out(rev: ProjectFilesRevision) -> ProjectFilesRevisionOut:
    base = rev.parent if rev.parent_id else None
    return ProjectFilesRevisionOut(
        number=rev.number, status=rev.status, message=rev.message, root_sha256=rev.root_sha256,
        commit=rev.commit, author=_who(rev.author_user),
        author_agent=rev.author_agent.slug if rev.author_agent_id else None,
        published_by=_who(rev.published_by) or None,
        published_at=rev.published_at.isoformat() if rev.published_at else None,
        created_at=rev.created_at.isoformat(),
        files=[{"path": p, "size": int(e.get("size") or 0), "sha256": e.get("sha256") or ""}
               for p, e in sorted((rev.tree or {}).items())],
        changes=pf.changes_between(base, rev))


@router.get(f"{_BASE}/", response=ProjectFilesOut,
            summary="A project's files: the live revision, the draft waiting to be published, and its skills")
@gate("member")
def get_project_files(request: HttpRequest, workspace: str, handle: str) -> ProjectFilesOut:
    """The draft is shown to those who may change the project; everyone who reads the
    project sees the live files."""
    project = project_ws_or_404(request, workspace, handle)
    live_rev = pf.live(project)
    editor = may_edit_project(request, project)
    pending = pf.draft(project) if editor else None
    return ProjectFilesOut(
        project=project.address, live=_revision_out(live_rev) if live_rev else None,
        draft=_revision_out(pending) if pending else None,
        skills=[{"name": n, **e} for n, e in sorted(pf.skills(live_rev).items())],
        may_draft=editor, may_publish=_may_publish(request, project), policy=list(pf.EDITABLE_GLOBS))


@router.get(f"{_BASE}/revisions/", response=list[ProjectFilesRevisionSummaryOut],
            summary="Every revision of a project's files, newest first")
@gate("member")
def list_project_file_revisions(request: HttpRequest, workspace: str, handle: str
                                ) -> list[ProjectFilesRevisionSummaryOut]:
    project = project_ws_or_404(request, workspace, handle)
    rows = ProjectFilesRevision.objects.filter(project=project).select_related("author_user", "published_by")
    if not may_edit_project(request, project):
        rows = rows.exclude(status__in=(pf.DRAFT, pf.DISCARDED))
    return [ProjectFilesRevisionSummaryOut(
        number=r.number, status=r.status, message=r.message, author=_who(r.author_user),
        published_by=_who(r.published_by) or None,
        published_at=r.published_at.isoformat() if r.published_at else None,
        created_at=r.created_at.isoformat()) for r in rows]


@router.get(f"{_BASE}/content", response={200: ProjectFileOut, **refusals(404)},
            summary="One file of a project, from its live files or a given revision")
@gate("member")
def get_project_file(request: HttpRequest, workspace: str, handle: str, path: str,
                     revision: int | None = None) -> ProjectFileOut:
    """`revision` omitted reads the live files. A draft or discarded revision is read only
    by those who may change the project."""
    project = project_ws_or_404(request, workspace, handle)
    rev = pf.revision(project, revision) if revision is not None else pf.live(project)
    if rev is not None and rev.status in (pf.DRAFT, pf.DISCARDED) and not may_edit_project(request, project):
        rev = None
    data = pf.read(rev, path) if rev is not None else None
    if data is None:
        raise HttpError(404, f"{project.address} has no file {path!r}"
                        + (f" in revision {revision}" if revision is not None else " live"))
    return ProjectFileOut(path=path, revision=rev.number, content=data.decode("utf-8", errors="replace"))


@router.put(f"{_BASE}/draft", response={200: ProjectFilesRevisionOut, **refusals(403, 409, 422)},
            summary="Make or replace a project's draft of its files")
# an editor of its workspace or its lead agent's, a person on it, or a working agent's own login
@gate("agent.work", "agent-login", "self", "project-member", kind="content")
def put_project_files_draft(request: HttpRequest, workspace: str, handle: str,
                            payload: ProjectFilesDraftIn) -> ProjectFilesRevisionOut:
    """`{base, changes: [{path, content | null}], message}`: the changes are made on the
    live revision `base` (0 while nothing is live) and replace any earlier draft. Nothing
    runs until a person who may publish it does. Every file is held to the policy
    (`policy` on `GET …/files/`): text only, no executable, nothing shaped like a secret."""
    project = project_ws_or_404(request, workspace, handle)
    if not may_edit_project(request, project):
        raise HttpError(403, "drafting a project's files requires the editor role, being on it, or being its agent")
    try:
        rev = pf.create_draft(project, base=payload.base, message=payload.message, by=request.user,
                              changes=[c.model_dump() for c in payload.changes])
    except pf.Refused as exc:
        raise _problem(exc) from None
    return _revision_out(rev)


@router.post(f"{_BASE}/draft/{{number}}/publish", response={200: ProjectFilesRevisionOut,
                                                             **refusals(403, 404, 409)},
             summary="Publish a project's draft of its files")
# its lead, an admin on it or a workspace owner, as a person: never an agent login or a turn's token
@gate("project-admin", "own", setting=True, step_up=True)  # browser: + step-up; a person's PAT allowed
def publish_project_files(request: HttpRequest, workspace: str, handle: str, number: int,
                          payload: ProjectFilesPublishIn) -> ProjectFilesRevisionOut:
    """What is published runs in the project's member agents' turns, with every vault
    attached to the project, so only the people those vaults open to publish it."""
    project = _publisher(request, workspace, handle)
    if stepup.is_browser(request):
        stepup.require_recent_step_up(request)
    before = pf.live(project)
    try:
        rev = pf.publish(project, number, by=request.user, expect_root=payload.expect_root)
    except pf.Refused as exc:
        raise _problem(exc) from None
    audit.note_change(scope=f"project:{project.address}/files",
                      before={"revision": before.number, "root_sha256": before.root_sha256} if before else None,
                      after={"revision": rev.number, "root_sha256": rev.root_sha256}, workspace=project.workspace,
                      summary=f"published revision {rev.number} of {project.address}'s files")
    return _revision_out(rev)


@router.delete(f"{_BASE}/draft/{{number}}", response={200: ProjectFilesRevisionOut, **refusals(403, 404, 409)},
               summary="Discard a project's draft of its files")
# its author, or whoever may publish it
@gate("agent.work", "agent-login", "self", "project-member", kind="content")
def discard_project_files_draft(request: HttpRequest, workspace: str, handle: str,
                                number: int) -> ProjectFilesRevisionOut:
    project = project_ws_or_404(request, workspace, handle)
    rev = pf.revision(project, number)
    if rev is None or not may_edit_project(request, project):
        raise HttpError(404, f"{project.address} has no draft {number}")
    if rev.author_user_id != getattr(request.user, "pk", None) and not _may_publish(request, project):
        raise HttpError(403, "a draft is discarded by whoever wrote it, or by someone who may publish it")
    try:
        return _revision_out(pf.discard(project, number))
    except pf.Refused as exc:
        raise _problem(exc) from None


@router.post(f"{_BASE}/revisions/{{number}}/rollback", response={200: ProjectFilesRevisionOut,
                                                                  **refusals(403, 409)},
             summary="Publish an earlier revision of a project's files again, as a new revision")
# its lead, an admin on it or a workspace owner, as a person: never an agent login or a turn's token
@gate("project-admin", "own", setting=True, step_up=True)  # browser: + step-up; a person's PAT allowed
def rollback_project_files(request: HttpRequest, workspace: str, handle: str, number: int,
                           payload: ProjectFilesRollbackIn) -> ProjectFilesRevisionOut:
    project = _publisher(request, workspace, handle)
    if stepup.is_browser(request):
        stepup.require_recent_step_up(request)
    before = pf.live(project)
    try:
        rev = pf.rollback(project, number, by=request.user, message=payload.message)
    except pf.Refused as exc:
        raise _problem(exc) from None
    audit.note_change(scope=f"project:{project.address}/files",
                      before={"revision": before.number, "root_sha256": before.root_sha256} if before else None,
                      after={"revision": rev.number, "root_sha256": rev.root_sha256}, workspace=project.workspace,
                      summary=f"rolled {project.address}'s files back to revision {number} (now {rev.number})")
    return _revision_out(rev)

"""Django Ninja router for canopy.origin issue records — /api/issues/.

Upsert (idempotent re-sync), list, retrieve, and DELETE (for cleanup). Keyed by `repo` + `number`;
the repo's slash is `__`-escaped in path params (`jjackson/canopy` -> `jjackson__canopy`).
"""
from __future__ import annotations

from django.http import HttpRequest
from ninja import Router, Status

from apps.agents.models import Agent
from apps.api.auth import session_auth
from apps.api.errors import TYPE_FORBIDDEN, TYPE_NOT_FOUND, TYPE_VALIDATION, ProblemError
from apps.api.pagination import Page, clamp_limit, clamp_offset, paginate
from apps.workspaces import permissions as perms
from apps.workspaces import services as wsvc

from .models import OriginIssue
from .schemas import OriginIssueIn, OriginIssueOut

router = Router(auth=session_auth, tags=["issues"])


def _out(obj: OriginIssue) -> OriginIssueOut:
    return OriginIssueOut.model_validate(obj)


def _unslug(repo_slug: str) -> str:
    return repo_slug.replace("__", "/")


def _visible(qs, request: HttpRequest):
    """Scope to the caller's workspaces (the hard tenant boundary)."""
    # A null-workspace row is in nobody's scope. It used to be in everybody's —
    # the NULL-means-allow leg — and `issues/0003` homed the rows it was keeping
    # visible, so dropping it hides nothing anyone legitimately reads.
    return qs.filter(workspace_id__in=wsvc.request_workspace_slugs(request))


def _require_editor(request: HttpRequest, workspace_id: str, verb: str) -> None:
    """Writing an origin record — filing, re-syncing or deleting one — is the
    author tier. A viewer reads the record."""
    if not perms.can(request.user, workspace_id, perms.CONTENT_WRITE):
        raise ProblemError(
            403, "Editor role required", type_=TYPE_FORBIDDEN,
            detail=f"{verb} an origin record requires the editor role in its workspace",
        )


def _get_or_404(request: HttpRequest, repo_slug: str, number: int) -> OriginIssue:
    repo = _unslug(repo_slug)
    obj = _visible(OriginIssue.objects.filter(repo=repo, number=number), request).first()
    if obj is None:
        raise ProblemError(
            404, "Issue record not found", type_=TYPE_NOT_FOUND,
            detail=f"No canopy.origin record for {repo}#{number}.",
        )
    return obj


def _assign_workspace(request: HttpRequest, agent_slug: str):
    """An origin record belongs to its authoring agent's workspace when the caller
    is a member of it (agent-authored provenance); otherwise a workspace the
    caller is ALREADY in — so nobody files a record into a workspace they're not
    in, and filing one is not itself a way to join a workspace. The fallback
    used to `ensure_member`; see wsvc.creation_workspace."""
    slugs = wsvc.request_workspace_slugs(request)
    agent = Agent.objects.filter(slug=agent_slug).first()
    if agent and agent.workspace_id in slugs:
        return agent.workspace
    return wsvc.creation_workspace(request)


@router.post("/", response={200: OriginIssueOut, 201: OriginIssueOut}, summary="Upsert an origin record")
def upsert_issue(request: HttpRequest, payload: OriginIssueIn) -> Status:
    data = payload.model_dump()
    repo = data.pop("repo")
    number = data.pop("number")

    # (repo, number) is globally unique — one origin record per GitHub issue. If a
    # record already exists in a workspace the caller isn't a member of, they must
    # not overwrite it: 404 (don't leak existence), same as read/delete. A record
    # with NO workspace is one nobody can see, so it 404s too rather than being
    # the one row anybody may overwrite.
    existing = OriginIssue.objects.filter(repo=repo, number=number).first()
    if existing and existing.workspace_id not in wsvc.request_workspace_slugs(request):
        raise ProblemError(
            404, "Issue record not found", type_=TYPE_NOT_FOUND,
            detail=f"No canopy.origin record for {repo}#{number}.",
        )
    if existing:
        _require_editor(request, existing.workspace_id, "re-syncing")

    defaults = dict(data)
    if existing is None:
        ws = _assign_workspace(request, data.get("agent") or "")
        if ws is None:
            # Never create an unhomed record: nobody could read it, and its
            # globally unique (repo, number) would then block every re-sync.
            raise ProblemError(
                422,
                "No workspace to file this record in",
                type_=TYPE_VALIDATION,
                detail=(
                    "you do not belong to a workspace that can own this; "
                    "ask an owner for an invite"
                ),
            )
        _require_editor(request, ws.slug, "filing")
        defaults["workspace"] = ws
    obj, created = OriginIssue.objects.update_or_create(repo=repo, number=number, defaults=defaults)
    return Status(201 if created else 200, _out(obj))


@router.get("/", response=Page[OriginIssueOut], summary="List origin records")
def list_issues(
    request: HttpRequest,
    initiative: str | None = None,
    repo: str | None = None,
    offset: int = 0,
    limit: int = 100,
) -> Page[OriginIssueOut]:
    offset, limit = clamp_offset(offset), clamp_limit(limit)
    qs = _visible(OriginIssue.objects.all(), request)
    if initiative:
        qs = qs.filter(initiative=initiative)
    if repo:
        qs = qs.filter(repo=repo)
    return paginate([_out(o) for o in qs], offset=offset, limit=limit)


@router.get("/{repo_slug}/{number}/", response=OriginIssueOut, summary="Get an origin record")
def get_issue(request: HttpRequest, repo_slug: str, number: int) -> OriginIssueOut:
    return _out(_get_or_404(request, repo_slug, number))


@router.delete("/{repo_slug}/{number}/", response={204: None}, summary="Delete an origin record (cleanup)")
def delete_issue(request: HttpRequest, repo_slug: str, number: int) -> Status:
    obj = _get_or_404(request, repo_slug, number)
    _require_editor(request, obj.workspace_id, "deleting")
    obj.delete()
    return Status(204, None)

"""Django Ninja v2 router for the DDD run views (read-only aggregation).

Mounted at ``/api/ddd``. A *narrative* is the run_id slug; runs roll up under
it. Everything here joins ``Walkthrough`` + ``ReviewRequest`` on ``run_id`` at
read time — see ``apps/runs/aggregate.py``. Team-internal: any authenticated
user (session or PAT) reads everything, same rule as the reviews dashboard.
"""
from __future__ import annotations

from django.http import HttpRequest
from ninja import Router, Status

from apps.api.auth import session_auth
from apps.api.errors import TYPE_FORBIDDEN, TYPE_NOT_FOUND, TYPE_VALIDATION, ProblemError
from apps.workspaces import services as wsvc

from . import aggregate, delete
from .transfer import TransferError, apply_move, plan_move, public_plan
from .schemas import (
    NarrativeDetailOut,
    NarrativeListItemOut,
    NarrativeMoveIn,
    NarrativeMoveOut,
    NarrativeVisibilityIn,
    NarrativeVisibilityOut,
    RunPackageOut,
    RunReleaseOut,
)

router = Router(auth=session_auth, tags=["ddd"])


def _workspace_slugs(request: HttpRequest) -> set[str]:
    """The caller's in-scope workspace slugs, mirroring the agents surface: a
    ``/api/w/{ws}/`` prefix pins one workspace (already membership-gated by
    ``WorkspaceResolveMiddleware``); a flat ``/api/`` call spans every workspace
    the user belongs to. Domain teammates are auto-joined first so the default
    workspace keeps resolving."""
    wsvc.auto_join_workspaces(request.user)
    ws = getattr(request, "workspace_slug", None)
    return {ws} if ws else wsvc.user_workspace_slugs(request.user)


@router.get(
    "/narratives/",
    response=list[NarrativeListItemOut],
    summary="List DDD narratives",
)
def list_narratives(
    request: HttpRequest, project: str = "", mine: str = ""
) -> list[NarrativeListItemOut]:
    owner_id = (
        request.user.id if (mine == "true" and request.user.is_authenticated) else None
    )
    items = aggregate.list_narratives(
        project=project.strip() or None,
        owner_id=owner_id,
        workspace_slugs=_workspace_slugs(request),
    )
    return [NarrativeListItemOut.model_validate(it) for it in items]


@router.get(
    "/narratives/{slug}/",
    response=NarrativeDetailOut,
    summary="Get a narrative + its runs",
)
def get_narrative(request: HttpRequest, slug: str) -> NarrativeDetailOut:
    data = aggregate.build_narrative(slug, workspace_slugs=_workspace_slugs(request))
    if data is None:
        raise ProblemError(404, "Narrative not found", type_=TYPE_NOT_FOUND)
    return NarrativeDetailOut.model_validate(data)


@router.get(
    "/runs/{run_id}/",
    response=RunPackageOut,
    summary="Get a run package (video + deck + narrative + links)",
)
def get_run(request: HttpRequest, run_id: str) -> RunPackageOut:
    data = aggregate.build_run(run_id, workspace_slugs=_workspace_slugs(request))
    if data is None:
        raise ProblemError(404, "Run not found", type_=TYPE_NOT_FOUND)
    return RunPackageOut.model_validate(data)


@router.get(
    "/release/{run_id}/",
    response=RunReleaseOut,
    auth=None,
    summary="Clean, shareable run release page (public via ?t=<share_token>)",
)
def get_run_release(request: HttpRequest, run_id: str) -> RunReleaseOut:
    """Anonymous-capable: the handler self-enforces access (workspace member OR a
    matching ``?t=`` share token) inside ``build_release`` — the middleware
    allowlist only lets the request reach here. Auto-join runs for authed members
    so their workspace membership resolves, mirroring the console endpoints."""
    if request.user.is_authenticated:
        wsvc.auto_join_workspaces(request.user)
    data = aggregate.build_release(run_id, request)
    if data is None:
        raise ProblemError(404, "Run not found", type_=TYPE_NOT_FOUND)
    return RunReleaseOut.model_validate(data)


@router.patch(
    "/narratives/{slug}/visibility/",
    response=NarrativeVisibilityOut,
    summary="Set visibility for an entire narrative (cascades to all artifacts + reviews)",
)
def set_narrative_visibility(
    request: HttpRequest, slug: str, payload: NarrativeVisibilityIn
) -> NarrativeVisibilityOut:
    slugs = _workspace_slugs(request)
    wt_n, rev_n = aggregate.set_narrative_visibility(
        slug, payload.visibility, workspace_slugs=slugs
    )
    detail = aggregate.build_narrative(slug, workspace_slugs=slugs)
    status = detail["visibility"] if detail else (
        "public" if payload.visibility == "link" else "private"
    )
    return NarrativeVisibilityOut(
        slug=slug,
        visibility=status,
        walkthroughs_updated=wt_n,
        reviews_updated=rev_n,
    )


# ---------------------------------------------------------------------------
# Cascade deletes
#
# Team-internal cleanup, same auth rule as the reviews dashboard: any
# authenticated user (session or PAT) may delete. Each delete cascades the
# rows that roll up under the target and best-effort removes rendered files
# from Drive — see ``apps/runs/delete.py``. 204 on success, 404 when nothing
# matched.
# ---------------------------------------------------------------------------


@router.delete(
    "/runs/{run_id}/",
    response={204: None},
    summary="Delete a run (its walkthroughs + reviews)",
)
def delete_run(request: HttpRequest, run_id: str):
    if delete.delete_run(run_id, workspace_slugs=_workspace_slugs(request)) is None:
        raise ProblemError(404, "Run not found", type_=TYPE_NOT_FOUND)
    return Status(204, None)


@router.delete(
    "/narratives/{slug}/versions/{version}/",
    response={204: None},
    summary="Delete a narrative version (and the runs under it)",
)
def delete_version(request: HttpRequest, slug: str, version: int):
    if delete.delete_version(
        slug, version, workspace_slugs=_workspace_slugs(request)
    ) is None:
        raise ProblemError(404, "Narrative version not found", type_=TYPE_NOT_FOUND)
    return Status(204, None)


@router.delete(
    "/narratives/{slug}/",
    response={204: None},
    summary="Delete an entire narrative (all versions + runs)",
)
def delete_narrative(request: HttpRequest, slug: str):
    if delete.delete_narrative(slug, workspace_slugs=_workspace_slugs(request)) is None:
        raise ProblemError(404, "Narrative not found", type_=TYPE_NOT_FOUND)
    return Status(204, None)


@router.post(
    "/narratives/{slug}/move/",
    response=NarrativeMoveOut,
    summary="Move a narrative (and its storyboards) to another workspace",
)
def move_narrative(request: HttpRequest, slug: str, payload: NarrativeMoveIn) -> dict:
    """Re-home a narrative — supported, not a repair script.

    A narrative is inferred from the rows that share its slug, so its workspace
    is the same answer repeated across every artifact with nothing keeping them
    in agreement. A version posted from a differently scoped caller splits the
    lineage across tenants and neither side can then read its own history. This
    heals that, and equally serves the honest case: the narrative turned out to
    belong to another team.

    Requires membership of BOTH sides — you may not move something out of a
    workspace you cannot see, nor into one you do not belong to.
    """
    slugs = {slug, *payload.also}
    mine = wsvc.user_workspace_slugs(request.user)

    if payload.to_workspace not in mine:
        raise ProblemError(
            403,
            f"You are not a member of {payload.to_workspace!r}",
            type_=TYPE_FORBIDDEN,
        )

    plan = plan_move(slugs, payload.to_workspace)
    unreachable = [ws for ws in plan["source_workspaces"] if ws not in mine]
    if unreachable:
        raise ProblemError(
            403,
            f"You are not a member of {', '.join(unreachable)} — "
            f"a narrative cannot be moved out of a workspace you cannot see",
            type_=TYPE_FORBIDDEN,
        )

    if not plan["narratives"]:
        raise ProblemError(404, "Narrative not found", type_=TYPE_NOT_FOUND)

    if payload.dry_run:
        return {**public_plan(plan), "dry_run": True}

    try:
        result = apply_move(slugs, payload.to_workspace)
    except TransferError as exc:
        raise ProblemError(422, str(exc), type_=TYPE_VALIDATION)
    return {**result, "dry_run": False}

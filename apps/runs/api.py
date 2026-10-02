"""Django Ninja v2 router for the DDD run views (read-only aggregation).

Mounted at ``/api/ddd``. A *narrative* is the run_id slug; runs roll up under
it. Everything here joins ``Walkthrough`` + ``ReviewRequest`` on ``run_id`` at
read time — see ``apps/runs/aggregate.py``. Members of a workspace read its
narratives; changing or deleting them is the editor tier.
"""
from __future__ import annotations

from django.http import HttpRequest
from ninja import Router, Status

from apps.api.auth import session_auth
from apps.api.errors import TYPE_FORBIDDEN, TYPE_NOT_FOUND, TYPE_VALIDATION, ProblemError
from apps.workspaces import permissions as perms
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
    the user belongs to."""
    ws = getattr(request, "workspace_slug", None)
    return {ws} if ws else wsvc.user_workspace_slugs(request.user)


def _editor_slugs(request: HttpRequest) -> set[str]:
    """The WRITE scope: the in-scope workspaces where the caller is an editor.

    Every mutation here is scoped by this rather than by `_workspace_slugs`. A
    narrative is a grouping over rows that may sit in several workspaces, so
    scoping by role (rather than refusing the whole call) means an editor of A
    who can also read B changes A's rows and leaves B's alone."""
    return perms.request_slugs_with(request, perms.CONTENT_WRITE)


def _forbidden(what: str) -> ProblemError:
    """A caller who can READ the target but holds no editor role over any of its
    rows. 403, not 404: they can already see it, so the refusal leaks nothing."""
    return ProblemError(
        403, f"{what} requires the editor role in its workspace", type_=TYPE_FORBIDDEN
    )


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
    allowlist only lets the request reach here."""
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
    editable = _editor_slugs(request)
    if (
        aggregate.build_narrative(slug, workspace_slugs=editable) is None
        and aggregate.build_narrative(slug, workspace_slugs=slugs) is not None
    ):
        raise _forbidden("Changing a narrative's visibility")
    wt_n, rev_n = aggregate.set_narrative_visibility(
        slug, payload.visibility, workspace_slugs=editable
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
# Workspace cleanup, the author tier: an EDITOR of the rows' workspace (session
# or PAT) may delete; a viewer who can see the target gets 403, anyone else 404.
# Each delete cascades the rows that roll up under the target and best-effort
# removes rendered files from Drive — see ``apps/runs/delete.py`` — which is
# why it is not the viewer tier. 204 on success, 404 when nothing matched.
# ---------------------------------------------------------------------------


@router.delete(
    "/runs/{run_id}/",
    response={204: None},
    summary="Delete a run (its walkthroughs + reviews)",
)
def delete_run(request: HttpRequest, run_id: str):
    if delete.delete_run(run_id, workspace_slugs=_editor_slugs(request)) is None:
        if aggregate.build_run(run_id, workspace_slugs=_workspace_slugs(request)) is not None:
            raise _forbidden("Deleting a run")
        raise ProblemError(404, "Run not found", type_=TYPE_NOT_FOUND)
    return Status(204, None)


@router.delete(
    "/narratives/{slug}/versions/{version}/",
    response={204: None},
    summary="Delete a narrative version (and the runs under it)",
)
def delete_version(request: HttpRequest, slug: str, version: int):
    if delete.delete_version(
        slug, version, workspace_slugs=_editor_slugs(request)
    ) is None:
        readable = aggregate.build_narrative(slug, workspace_slugs=_workspace_slugs(request))
        if readable and any(v.get("version") == version for v in readable["versions"]):
            raise _forbidden("Deleting a narrative version")
        raise ProblemError(404, "Narrative version not found", type_=TYPE_NOT_FOUND)
    return Status(204, None)


@router.delete(
    "/narratives/{slug}/",
    response={204: None},
    summary="Delete an entire narrative (all versions + runs)",
)
def delete_narrative(request: HttpRequest, slug: str):
    if delete.delete_narrative(slug, workspace_slugs=_editor_slugs(request)) is None:
        if aggregate.build_narrative(slug, workspace_slugs=_workspace_slugs(request)) is not None:
            raise _forbidden("Deleting a narrative")
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

    Requires the editor role on BOTH sides — you may not move something out of
    a workspace you cannot change, nor into one where you could not have
    created it.
    """
    slugs = {slug, *payload.also}
    # Editor, not membership: a move takes the narrative out of its source as
    # surely as delete_narrative does, and lands rows in the destination as
    # surely as an upload does — both are author-tier acts.
    mine = wsvc.user_workspace_slugs(request.user)
    editable = {ws for ws in mine if perms.can(request.user, ws, perms.CONTENT_WRITE)}

    if payload.to_workspace not in editable:
        raise ProblemError(
            403,
            f"You need the editor role in {payload.to_workspace!r} to move a narrative into it",
            type_=TYPE_FORBIDDEN,
        )

    plan = plan_move(slugs, payload.to_workspace)
    held_in = {
        str(row.workspace_id)
        for row in (*plan["_reviews"], *plan["_walkthroughs"], *plan["_boards"])
    }
    if not plan["narratives"] or not held_in & mine:
        # Nothing of it is in a workspace the caller is in: as far as they can
        # tell, it does not exist.
        raise ProblemError(404, "Narrative not found", type_=TYPE_NOT_FOUND)

    unreachable = [ws for ws in plan["source_workspaces"] if ws not in editable]
    if unreachable:
        # Name only the workspaces the caller is in. Naming one they are not in
        # would tell them a tenant they cannot see holds a narrative by this name.
        seen = [ws for ws in unreachable if ws in mine]
        where = ", ".join(seen) if seen else "a workspace you are not a member of"
        raise ProblemError(
            403,
            f"You need the editor role in {where} — "
            f"a narrative cannot be moved out of a workspace you cannot change",
            type_=TYPE_FORBIDDEN,
        )

    if payload.dry_run:
        return {**public_plan(plan), "dry_run": True}

    try:
        result = apply_move(slugs, payload.to_workspace)
    except TransferError as exc:
        raise ProblemError(422, str(exc), type_=TYPE_VALIDATION)
    return {**result, "dry_run": False}

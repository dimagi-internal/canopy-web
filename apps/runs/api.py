"""Django Ninja v2 router for the DDD run views (read-only aggregation).

Mounted at ``/api/ddd``. A *narrative* is the run_id slug; runs roll up under
it. Everything here joins ``Walkthrough`` + ``ReviewRequest`` on ``run_id`` at
read time — see ``apps/runs/aggregate.py``. Members of a workspace read its
narratives; changing or deleting them is the editor tier.
"""
from __future__ import annotations

import uuid

from django.conf import settings
from django.http import HttpRequest, HttpResponse
from ninja import File, Form, Router, Status
from ninja.files import UploadedFile

from apps.api.auth import session_auth
from apps.api.errors import (
    TYPE_DRIVE_NOT_CONFIGURED,
    TYPE_DRIVE_UPLOAD_FAILED,
    TYPE_FORBIDDEN,
    TYPE_NOT_FOUND,
    TYPE_PAYLOAD_TOO_LARGE,
    TYPE_VALIDATION,
    ProblemError,
)
from apps.api.route_gates import gate
from apps.walkthroughs import storage
from apps.walkthroughs.drive_client import DriveNotConfigured
from apps.workspaces import permissions as perms
from apps.workspaces import services as wsvc

from . import aggregate, delete, sources
from .schemas import (
    NarrativeDetailOut,
    NarrativeListItemOut,
    NarrativeMoveIn,
    NarrativeMoveOut,
    NarrativeSourceOut,
    NarrativeSourceRole,
    NarrativeSourceUploadOut,
    NarrativeVisibilityIn,
    NarrativeVisibilityOut,
    RunPackageOut,
    RunReleaseOut,
)
from .transfer import TransferError, apply_move, plan_move, public_plan

router = Router(auth=session_auth, tags=["ddd"])


def _workspace_slugs(request: HttpRequest) -> set[str]:
    """The caller's in-scope workspace slugs, mirroring the agents surface: a
    ``/api/w/{ws}/`` prefix pins one workspace (already membership-gated by
    ``WorkspaceResolveMiddleware``); a flat ``/api/`` call spans every workspace
    the user belongs to — when those sit in ONE tenant tree. A narrative slug or
    run id is a NAME that groups rows across workspaces, not a row id, so every
    read here takes the read rule (`wsvc.read_scope`): a caller in more than one
    tree who pins none gets a 422 rather than two organisations' rows merged
    under one name."""
    return wsvc.request_workspace_slugs(request)


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
@gate("member")
def list_narratives(
    request: HttpRequest, project: str = "", mine: str = "", session: str = "",
    agent_project: str = "",
) -> list[NarrativeListItemOut]:
    owner_id = (
        request.user.id if (mine == "true" and request.user.is_authenticated) else None
    )
    items = aggregate.list_narratives(
        project=project.strip() or None,
        owner_id=owner_id,
        workspace_slugs=_workspace_slugs(request),
        session=session.strip() or None,
        agent_project=agent_project.strip() or None,
    )
    return [NarrativeListItemOut.model_validate(it) for it in items]


@router.get(
    "/narratives/{slug}/",
    response=NarrativeDetailOut,
    summary="Get a narrative + its runs",
)
@gate("member")
def get_narrative(request: HttpRequest, slug: str) -> NarrativeDetailOut:
    data = aggregate.build_narrative(slug, workspace_slugs=_workspace_slugs(request))
    if data is None:
        raise ProblemError(404, "Narrative not found", type_=TYPE_NOT_FOUND)
    from apps.feedback import reactions

    reactions.record_view(request, reactions.NARRATIVE, slug)
    return NarrativeDetailOut.model_validate(data)


@router.get(
    "/runs/{run_id}/",
    response=RunPackageOut,
    summary="Get a run package (video + deck + narrative + links)",
)
@gate("member")
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
@gate("token-link")  # auth=None; member or ?t= on the primary artifact
def get_run_release(request: HttpRequest, run_id: str, ws: str = "") -> RunReleaseOut:
    """Anonymous-capable: the handler self-enforces access (workspace member OR a
    matching ``?t=`` share token) inside ``build_release`` — the middleware
    allowlist only lets the request reach here. ``ws`` is the workspace the
    page's URL names (``/w/<ws>/ddd-release/…``); a run elsewhere 404s."""
    data = aggregate.build_release(run_id, request, ws)
    if data is None:
        raise ProblemError(404, "Run not found", type_=TYPE_NOT_FOUND)
    return RunReleaseOut.model_validate(data)


@router.patch(
    "/narratives/{slug}/visibility/",
    response=NarrativeVisibilityOut,
    summary="Set visibility for an entire narrative (cascades to all artifacts + reviews)",
)
@gate("content.write", setting=True)  # scoped by editor slugs
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
@gate("content.write", kind="content")
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
@gate("content.write", kind="content")
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
@gate("content.write", kind="content")
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
@gate("content.write", kind="content")  # editor on source AND destination
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
    editable = perms.slugs_with(request.user, perms.CONTENT_WRITE, within=mine)

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


# ---------------------------------------------------------------------------
# Iteration sources
#
# The files a narrative needs to be iterated again from any machine — recipe,
# master recording, capture report — pinned to the version they were made for
# (apps/runs/sources.py). Members read them; an editor of the version's
# workspace writes them.
# ---------------------------------------------------------------------------


def _versions_by_review(slug: str, workspace_slugs: set[str]) -> dict[str, int | None]:
    data = aggregate.build_narrative(slug, workspace_slugs=workspace_slugs) or {}
    return {
        v["review_id"]: v.get("version")
        for v in data.get("versions", [])
        if v.get("review_id")
    }


@router.post(
    "/narratives/{slug}/sources/",
    response={201: NarrativeSourceUploadOut},
    summary="Pin an iteration source (recipe, master, capture report) to a narrative version",
)
@gate("content.write", kind="content")  # editor of the pinned version's workspace
def upload_narrative_source(
    request: HttpRequest,
    slug: str,
    file: UploadedFile = File(...),
    role: NarrativeSourceRole = Form(...),
    narrative_review_id: uuid.UUID = Form(...),
) -> Status:
    """Stores a file the narrative needs in order to be iterated again, so that
    nothing required to keep iterating lives only on the machine that made the
    render. Identical bytes already pinned to the version are reused; a changed
    file replaces the version's previous one for that role."""
    max_bytes = getattr(settings, "NARRATIVE_SOURCE_MAX_UPLOAD_BYTES", 150 * 1024 * 1024)
    if file.size > max_bytes:
        raise ProblemError(
            413, "Payload too large", type_=TYPE_PAYLOAD_TOO_LARGE,
            detail=f"A narrative source may be at most {max_bytes} bytes.",
        )
    review = sources.version_review(slug, narrative_review_id, _editor_slugs(request))
    if review is None:
        if sources.version_review(slug, narrative_review_id, _workspace_slugs(request)):
            raise _forbidden("Adding a narrative source")
        raise ProblemError(
            404, "Narrative version not found", type_=TYPE_NOT_FOUND,
            detail=f"{narrative_review_id} is not a version of narrative {slug!r}",
        )
    try:
        src, reused = sources.store(
            review=review, slug=slug, role=role, data=file.read(), owner=request.user,
        )
    except DriveNotConfigured as exc:
        raise ProblemError(
            500, "Drive not configured", type_=TYPE_DRIVE_NOT_CONFIGURED, detail=str(exc),
        )
    except Exception as exc:  # noqa: BLE001 — surfaced as a 502, row already rolled back
        raise ProblemError(
            502, "Drive upload failed", type_=TYPE_DRIVE_UPLOAD_FAILED, detail=str(exc),
        )
    payload = sources.detail_payload(src, {str(review.id): review.version})
    return Status(201, NarrativeSourceUploadOut.model_validate({**payload, "reused": reused}))


@router.get(
    "/narratives/{slug}/sources/",
    response=list[NarrativeSourceOut],
    summary="List a narrative's iteration sources (newest first)",
)
@gate("member")
def list_narrative_sources(request: HttpRequest, slug: str) -> list[NarrativeSourceOut]:
    scope = _workspace_slugs(request)
    rows = sources.for_slug(slug, scope)
    versions = _versions_by_review(slug, scope)
    return [NarrativeSourceOut.model_validate(sources.detail_payload(s, versions)) for s in rows]


@router.get(
    "/narratives/{slug}/sources/{source_id}/content",
    summary="Download one iteration source's bytes",
)
@gate("member")
def get_narrative_source_content(request: HttpRequest, slug: str, source_id: uuid.UUID):
    src = next(
        (s for s in sources.for_slug(slug, _workspace_slugs(request)) if s.id == source_id),
        None,
    )
    if src is None or not src.drive_file_id:
        raise ProblemError(404, "Narrative source not found", type_=TYPE_NOT_FOUND)
    try:
        data, _start, _end, _total = storage.download(file_id=src.drive_file_id)
    except DriveNotConfigured as exc:
        raise ProblemError(
            500, "Drive not configured", type_=TYPE_DRIVE_NOT_CONFIGURED, detail=str(exc),
        )
    resp = HttpResponse(data, content_type=src.content_type)
    resp["Content-Disposition"] = f'attachment; filename="{src.filename}"'
    resp["X-Content-SHA256"] = src.sha256
    return resp

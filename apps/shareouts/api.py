"""Django Ninja router for the /api/shareouts surface."""
from __future__ import annotations

import datetime as dt

from django.http import HttpRequest
from ninja import Router, Status

from apps.api.auth import session_auth
from apps.api.errors import TYPE_FORBIDDEN, TYPE_VALIDATION, ProblemError
from apps.api.pagination import Page, clamp_limit, paginate
from apps.workspaces import services as wsvc

from . import services
from .schemas import (
    ShareoutBatchIn,
    ShareoutBatchOut,
    ShareoutOut,
    ShareoutsClearIn,
    ShareoutsClearOut,
)

router = Router(auth=session_auth, tags=["shareouts"])


@router.get(
    "/",
    response=Page[ShareoutOut],
    summary="List shareouts",
)
def list_shareouts(
    request: HttpRequest,
    date_from: dt.date | None = None,
    date_to: dt.date | None = None,
    project: str | None = None,
    limit: int = 100,
) -> Page[ShareoutOut]:
    """List dated work briefings, newest period first. Filters AND-combine.

    Tenant-scoped: on a /w/{ws} request, only that workspace's rows; on the flat
    mount, every workspace the caller is a member of (the PAT resolves to a real
    user, so machine producers see their tenant's rows too)."""
    limit = clamp_limit(limit)
    ws = getattr(request, "workspace_slug", None)
    slugs = {ws} if ws else wsvc.user_workspace_slugs(request.user)
    rows = services.list_shareouts(
        date_from=date_from,
        date_to=date_to,
        project=project,
        limit=limit,
        workspace_slugs=slugs,
    )
    items = [ShareoutOut.model_validate(row) for row in rows]
    return paginate(items, offset=0, limit=limit)


@router.post(
    "/",
    response={201: ShareoutBatchOut},
    summary="Create shareouts (batch, idempotent per period+source)",
)
def create_shareouts(
    request: HttpRequest,
    payload: ShareoutBatchIn,
) -> Status:
    """Create a batch of briefings. Re-posting the same period from the same
    source replaces YOUR prior rows for it (see services.upsert_shareouts);
    a teammate's briefing for the same period is left alone. Requires the
    editor role.

    Rows are assigned to a workspace you already belong to: the `/w/{ws}` prefix
    pins it, otherwise it resolves to your default. 422 if you belong to none.
    """
    # Resolution is `wsvc.creation_workspace`, which only ever returns a tenant
    # the caller is already in. This used to be `pinned or
    # ensure_default_workspace()` followed by `ensure_member(ws, request.user)`
    # — which on the flat mount made posting a shareout a way to BECOME an
    # editor of the org default. See that helper's docstring for the full shape.
    ws = wsvc.creation_workspace(request)
    if ws is None:
        raise ProblemError(
            422,
            "No workspace to post shareouts in",
            type_=TYPE_VALIDATION,
            detail="you do not belong to a workspace that can own this; ask an owner for an invite",
        )
    if not wsvc.has_role_at_least(request.user, ws, wsvc.WorkspaceMembership.EDITOR):
        raise ProblemError(
            403,
            "Editor role required",
            type_=TYPE_FORBIDDEN,
            detail=f"posting a shareout requires the editor role in {ws.slug!r}",
        )
    result = services.upsert_shareouts(payload.shareouts, workspace=ws, created_by=request.user)
    return Status(201, ShareoutBatchOut(**result))


@router.post(
    "/clear/",
    response=ShareoutsClearOut,
    summary="Clear shareouts by source / project / date (AND-combined)",
)
def clear_shareouts(
    request: HttpRequest,
    payload: ShareoutsClearIn,
) -> ShareoutsClearOut:
    """Delete shareouts matching the filters. You clear the shareouts you
    posted in workspaces where you are an editor, and every shareout in a
    workspace you own — never a teammate's otherwise, and never another
    tenant's. An empty body clears all of those (the pinned /w/{ws} one, or the
    union of your memberships)."""
    # The docstring always said "the caller's", but the query was every row in
    # every workspace the caller could READ — so a viewer's `{}` wiped the feed.
    owned = wsvc.request_workspace_slugs_at_least(request, wsvc.WorkspaceMembership.OWNER)
    edited = wsvc.request_workspace_slugs_at_least(request, wsvc.WorkspaceMembership.EDITOR)
    count = services.clear_shareouts(
        workspace_slugs=owned,
        own_only_slugs=edited - owned,
        user=request.user,
        source=payload.source,
        project=payload.project,
        date_from=payload.date_from,
        date_to=payload.date_to,
    )
    return ShareoutsClearOut(cleared=count)

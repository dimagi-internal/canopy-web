"""Django Ninja router for the /api/shareouts surface."""
from __future__ import annotations

import datetime as dt

from django.http import HttpRequest
from ninja import Router, Status

from apps.api.auth import session_auth
from apps.api.errors import TYPE_VALIDATION, ProblemError
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
    openapi_extra={"x-mcp-expose": True},
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
    openapi_extra={"x-mcp-expose": True},
)
def create_shareouts(
    request: HttpRequest,
    payload: ShareoutBatchIn,
) -> Status:
    """Create a batch of briefings. Re-posting the same period from the same
    source replaces the prior rows (see services.upsert_shareouts).

    Rows are assigned to a workspace the caller is ALREADY in — the /w{ws}
    prefix pins it, else the org default when they are a member of it, else
    their sole membership (`wsvc.creation_workspace`). This used to call
    `ensure_member`, which made posting a shareout a way to BECOME an editor of
    the org default; see that helper's docstring."""
    ws = wsvc.creation_workspace(request)
    if ws is None:
        raise ProblemError(
            422,
            "No workspace to post shareouts in",
            type_=TYPE_VALIDATION,
            detail="you do not belong to a workspace that can own this; ask an owner for an invite",
        )
    result = services.upsert_shareouts(payload.shareouts, workspace=ws)
    return Status(201, ShareoutBatchOut(**result))


@router.post(
    "/clear/",
    response=ShareoutsClearOut,
    summary="Clear shareouts by source / project / date (AND-combined)",
    openapi_extra={"x-mcp-expose": True},
)
def clear_shareouts(
    request: HttpRequest,
    payload: ShareoutsClearIn,
) -> ShareoutsClearOut:
    """Delete shareouts matching the filters, scoped to the caller's workspaces.
    An empty body clears all of THE CALLER'S shareouts (the pinned /w/{ws} one, or
    the union of their memberships) — never another tenant's."""
    count = services.clear_shareouts(
        workspace_slugs=wsvc.request_workspace_slugs(request),
        source=payload.source,
        project=payload.project,
        date_from=payload.date_from,
        date_to=payload.date_to,
    )
    return ShareoutsClearOut(cleared=count)

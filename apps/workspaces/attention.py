"""Requests to join a workspace, waiting on the people who may let someone in
(`members.manage`)."""
from __future__ import annotations

from apps.attention.types import AttentionRow

from . import permissions as perms
from .models import WorkspaceAccessRequest
from .services import scoped_path


def items(request, *, slugs: set[str]) -> list[AttentionRow]:
    managed = perms.slugs_with(request.user, perms.MEMBERS_MANAGE, within=slugs)
    qs = WorkspaceAccessRequest.objects.filter(
        workspace_id__in=managed, status=WorkspaceAccessRequest.PENDING,
    ).select_related("user")[:200]
    return [
        AttentionRow(
            key=f"access:{r.pk}",
            kind="access_request",
            reason="Wants to join",
            title=f"{r.user.get_full_name() or r.user.email} asked to join {r.workspace_id}",
            body=r.note[:400],
            workspace=r.workspace_id,
            url=scoped_path(r.workspace_id, f"/settings/access-requests/{r.pk}"),
            last_activity_at=r.created_at,
            actions=("open",),
            target={"request": str(r.pk)},
        )
        for r in qs
    ]

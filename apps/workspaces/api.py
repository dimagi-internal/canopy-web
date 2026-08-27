"""Django Ninja router for /api/workspaces — the multi-tenancy surface.

Membership-scoped: a workspace is visible only to its members; a non-member
gets 404 (no existence leak). Creating a workspace makes the creator its owner.
Owners manage members + invites (RBAC via `_require_role`); invites are accepted
by token, only by the addressed email.
"""
from __future__ import annotations

from django.http import HttpRequest
from ninja import Router, Status
from ninja.errors import HttpError

from apps.api.auth import session_auth

from . import services
from .models import Workspace, WorkspaceInvite, WorkspaceMembership
from .schemas import (
    InviteCreateIn,
    InviteOut,
    InvitePreviewOut,
    MemberOut,
    MemberRoleUpdateIn,
    WorkspaceCreateIn,
    WorkspaceOut,
)

router = Router(auth=session_auth, tags=["workspaces"])

# InviteError.code -> HTTP status. Preserves the status codes the views
# returned before this logic moved into `services.py`.
_INVITE_ERROR_STATUS = {
    "not_found": 404,
    "expired": 410,
    "revoked": 410,
    "already_accepted": 410,
    "email_mismatch": 403,
}

# MemberError.code -> HTTP status.
_MEMBER_ERROR_STATUS = {
    "not_found": 404,
    "last_owner": 400,
    "invalid_role": 422,
}

# MemberError.code -> a human-readable message, PER ENDPOINT (the same code
# reads differently depending on the verb that tripped it — "cannot remove"
# vs "cannot demote" — so this is deliberately two small dicts, not one
# shared across both routes). Never surface `exc.code` itself to a user: it's
# a machine token for the status lookup above, not banner copy.
_REMOVE_MEMBER_ERROR_MESSAGES = {
    "not_found": "member not found",
    "last_owner": "cannot remove the last owner",
}
_SET_MEMBER_ROLE_ERROR_MESSAGES = {
    "not_found": "member not found",
    "last_owner": "cannot demote the last owner",
    "invalid_role": "unknown role",
}


def _out(ws: Workspace, role: str) -> WorkspaceOut:
    return WorkspaceOut(
        slug=ws.slug,
        display_name=ws.display_name,
        auto_join_domains=ws.auto_join_domains,
        role=role,
        created_at=ws.created_at,
    )


def _membership_or_404(user, slug: str) -> WorkspaceMembership:
    try:
        return WorkspaceMembership.objects.select_related("workspace").get(
            workspace_id=slug, user=user
        )
    except WorkspaceMembership.DoesNotExist:
        raise HttpError(404, f"workspace '{slug}' not found")


def _require_role(user, slug: str, *allowed: str) -> WorkspaceMembership:
    m = _membership_or_404(user, slug)  # 404 first — a non-member can't probe roles
    if m.role not in allowed:
        raise HttpError(403, f"requires one of roles {list(allowed)}")
    return m


def _member_out(m: WorkspaceMembership) -> MemberOut:
    return MemberOut(user_id=m.user_id, email=m.user.email, role=m.role, joined_at=m.joined_at)


def _invite_out(inv: WorkspaceInvite) -> InviteOut:
    return InviteOut(
        id=inv.id, email=inv.email, role=inv.role, token=inv.token,
        expires_at=inv.expires_at, accepted_at=inv.accepted_at, revoked_at=inv.revoked_at,
    )


@router.post("/", response={201: WorkspaceOut}, summary="Create a workspace",
             openapi_extra={"x-mcp-expose": True})
def create_workspace(request: HttpRequest, payload: WorkspaceCreateIn) -> Status:
    # An invite-admitted user (cleared the OAuth gate via a live invite or an
    # existing membership, not the domain allowlist) must not be able to
    # bootstrap their own workspace and mint invites of their own — that
    # would make invite-admission transitively delegable to an attacker. See
    # `services.can_create_workspace` + the F1 security finding.
    if not services.can_create_workspace(request.user):
        raise HttpError(403, "not eligible to create a workspace")
    if Workspace.objects.filter(slug=payload.slug).exists():
        raise HttpError(409, f"workspace '{payload.slug}' already exists")
    ws = Workspace.objects.create(
        slug=payload.slug,
        display_name=payload.display_name,
        created_by=request.user,
        # auto_join_domains is deliberately NOT settable from the request —
        # see WorkspaceCreateIn. Only `ensure_default_workspace()` sets it.
    )
    WorkspaceMembership.objects.create(
        workspace=ws, user=request.user, role=WorkspaceMembership.OWNER
    )
    return Status(201, _out(ws, WorkspaceMembership.OWNER))


@router.get("/", response=list[WorkspaceOut], summary="List my workspaces",
            openapi_extra={"x-mcp-expose": True})
def list_workspaces(request: HttpRequest) -> list[WorkspaceOut]:
    memberships = (
        WorkspaceMembership.objects.filter(user=request.user)
        .select_related("workspace")
        .order_by("-workspace__created_at")
    )
    return [_out(m.workspace, m.role) for m in memberships]


@router.get("/{slug}/", response=WorkspaceOut, summary="Get a workspace (member-only)",
            openapi_extra={"x-mcp-expose": True})
def get_workspace(request: HttpRequest, slug: str) -> WorkspaceOut:
    m = _membership_or_404(request.user, slug)
    return _out(m.workspace, m.role)


@router.delete("/{slug}/", response={204: None}, summary="Delete a workspace (owner-only)",
               openapi_extra={"x-mcp-expose": True})
def delete_workspace(request: HttpRequest, slug: str):
    """Delete an empty workspace. Owner-only, and never one that still owns agents.

    Creation (`POST /`) was a one-way door: a workspace made with a typo'd
    slug was permanent, and the slug appears in every URL its team uses. That
    is a bad property for a self-serve create endpoint, and it made the
    onboarding path un-rehearsable for the same reason agent creation was.

    Two guards, both deliberate:

    - **Owner-only**, matching the rest of workspace administration (invites,
      member roles). An editor may act *within* a tenant; removing the tenant
      itself is an owner's call.
    - **Refuses while any agent still lives here.** `Agent.workspace` is
      PROTECT precisely so a tenant cannot be pulled out from under its
      agents, and Django would raise ProtectedError — a 500. Checking first
      turns that into an actionable 409 naming what is in the way, so the
      caller deletes the agents (or moves them) and retries. Memberships and
      invites are the workspace's own bookkeeping and cascade with it.
    """
    _require_role(request.user, slug, WorkspaceMembership.OWNER)
    ws = Workspace.objects.filter(slug=slug).first()
    if ws is None:
        raise HttpError(404, f"workspace '{slug}' not found")
    agent_slugs = sorted(ws.agents.values_list("slug", flat=True))
    if agent_slugs:
        raise HttpError(
            409,
            f"workspace '{slug}' still owns {len(agent_slugs)} agent(s): "
            f"{', '.join(agent_slugs)}. Delete or move them first.",
        )
    ws.delete()
    return Status(204, None)


# ---- members ----
@router.get("/{slug}/members/", response=list[MemberOut], summary="List members (member-only)",
            openapi_extra={"x-mcp-expose": True})
def list_members(request: HttpRequest, slug: str) -> list[MemberOut]:
    _membership_or_404(request.user, slug)
    members = (
        WorkspaceMembership.objects.filter(workspace_id=slug)
        .select_related("user").order_by("joined_at")
    )
    return [_member_out(m) for m in members]


@router.delete("/{slug}/members/{user_id}/", response={204: None},
               summary="Remove a member (owner-only)", openapi_extra={"x-mcp-expose": True})
def remove_member(request: HttpRequest, slug: str, user_id: int):
    m = _require_role(request.user, slug, WorkspaceMembership.OWNER)
    try:
        services.remove_member(workspace=m.workspace, user_id=user_id)
    except services.MemberError as exc:
        raise HttpError(_MEMBER_ERROR_STATUS[exc.code], _REMOVE_MEMBER_ERROR_MESSAGES[exc.code])
    return Status(204, None)


@router.patch("/{slug}/members/{user_id}/", response=MemberOut,
              summary="Change a member's role (owner-only)", openapi_extra={"x-mcp-expose": True})
def set_member_role(request: HttpRequest, slug: str, user_id: int, payload: MemberRoleUpdateIn) -> MemberOut:
    m = _require_role(request.user, slug, WorkspaceMembership.OWNER)
    try:
        updated = services.set_member_role(workspace=m.workspace, user_id=user_id, role=payload.role)
    except services.MemberError as exc:
        raise HttpError(_MEMBER_ERROR_STATUS[exc.code], _SET_MEMBER_ROLE_ERROR_MESSAGES[exc.code])
    return _member_out(updated)


# ---- invites ----
@router.post("/{slug}/invites/", response={201: InviteOut}, summary="Invite by email (owner-only)",
             openapi_extra={"x-mcp-expose": True})
def create_invite(request: HttpRequest, slug: str, payload: InviteCreateIn) -> Status:
    m = _require_role(request.user, slug, WorkspaceMembership.OWNER)
    inv = services.create_invite(
        workspace=m.workspace, email=payload.email, role=payload.role, invited_by=request.user,
    )
    return Status(201, _invite_out(inv))


@router.get("/{slug}/invites/", response=list[InviteOut], summary="List invites (member-only)",
            openapi_extra={"x-mcp-expose": True})
def list_invites(request: HttpRequest, slug: str) -> list[InviteOut]:
    _membership_or_404(request.user, slug)
    return [_invite_out(i) for i in WorkspaceInvite.objects.filter(workspace_id=slug).order_by("-created_at")]


@router.post("/{slug}/invites/{invite_id}/revoke", response={204: None},
             summary="Revoke an invite (owner-only)", openapi_extra={"x-mcp-expose": True})
def revoke_invite(request: HttpRequest, slug: str, invite_id: int):
    _require_role(request.user, slug, WorkspaceMembership.OWNER)
    try:
        inv = WorkspaceInvite.objects.get(workspace_id=slug, id=invite_id)
    except WorkspaceInvite.DoesNotExist:
        raise HttpError(404, "invite not found")
    services.revoke_invite(invite=inv)
    return Status(204, None)


@router.get("/invites/{token}/preview", response=InvitePreviewOut, auth=None,
            summary="Preview an invite before login (pre-auth, minimal disclosure)")
def preview_invite(request: HttpRequest, token: str) -> InvitePreviewOut:
    try:
        inv = WorkspaceInvite.objects.select_related("workspace").get(token=token)
    except WorkspaceInvite.DoesNotExist:
        raise HttpError(404, "invite not found")
    status = services.invite_status(inv)
    hint = services.mask_email(inv.email)
    if status != "pending":
        # Minimal disclosure: a dead token (expired/revoked/accepted) reveals
        # only that it's dead, never which workspace it pointed at.
        return InvitePreviewOut(status=status, email_hint=hint)
    return InvitePreviewOut(
        status=status,
        email_hint=hint,
        workspace_slug=inv.workspace.slug,
        workspace_display_name=inv.workspace.display_name,
        role=inv.role,
    )


@router.post("/invites/{token}/accept", response=WorkspaceOut,
             summary="Accept an invite by token", openapi_extra={"x-mcp-expose": True})
def accept_invite(request: HttpRequest, token: str) -> WorkspaceOut:
    try:
        ws, role = services.accept_invite(token=token, user=request.user)
    except services.InviteError as exc:
        status = _INVITE_ERROR_STATUS[exc.code]
        raise HttpError(status, exc.code)
    return _out(ws, role)

"""Django Ninja router for /api/workspaces — the multi-tenancy surface.

Membership-scoped: a workspace is visible only to its members; a non-member
gets 404 (no existence leak). Creating a workspace makes the creator its owner.
Admins and owners manage members + invites (`_require` + `permissions`); invites are accepted
by token, only by the addressed email. People whose login email is at one of a
workspace's `access_request_domains` may REQUEST an invitation; an admin or owner
approves or denies it (docs/architecture/access.md, "Getting into a workspace").
"""
from __future__ import annotations

from django.http import HttpRequest
from ninja import Router, Status
from ninja.errors import HttpError

from apps.workspaces import permissions as perms
from apps.api.auth import session_auth

from . import services
from .models import Workspace, WorkspaceAccessRequest, WorkspaceInvite, WorkspaceMembership
from .schemas import (
    AccessRequestApproveIn,
    AccessRequestDenyIn,
    AccessRequestIn,
    AccessRequestOut,
    AccessSettingsIn,
    InviteCreateIn,
    InviteOut,
    InvitePreviewOut,
    RequestableWorkspaceOut,
    MemberOut,
    MemberRoleUpdateIn,
    RunnerOrderIn,
    RunnerOrderRowOut,
    RunnerTopologyOut,
    AgentTopologyOut,
    SharedVaultIn,
    SharedVaultOut,
    WorkspaceCreateIn,
    WorkspaceOut,
    WorkspaceParentIn,
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


def _out(ws: Workspace, role: str, *, inherited: bool = False) -> WorkspaceOut:
    return WorkspaceOut(
        slug=ws.slug,
        display_name=ws.display_name,
        access_request_domains=ws.access_request_domains,
        auto_approve_role=ws.auto_approve_role,
        role=role,
        created_at=ws.created_at,
        parent=ws.parent_id,
        inherited=inherited,
    )


def _m_out(m: WorkspaceMembership) -> WorkspaceOut:
    return _out(m.workspace, m.role, inherited=getattr(m, "inherited", False))


def _membership_or_404(user, slug: str) -> WorkspaceMembership:
    """The caller's membership row, or 404.

    Reads through `services.membership` rather than querying here: this surface
    needs the ROW (to read its workspace and to mutate it), but "am I in this
    workspace?" must still have one implementation — see
    `tests/test_workspace_authorizer_is_sole_gate.py`.
    """
    m = services.membership(user, slug)
    if m is None:
        raise HttpError(404, f"workspace '{slug}' not found")
    return m


def _require(user, slug: str, capability: str) -> WorkspaceMembership:
    """The caller's membership, if their role holds `capability`
    (`permissions.MINIMUM_ROLE`). 404 first — a non-member can't probe roles."""
    m = _membership_or_404(user, slug)  # 404 first — a non-member can't probe roles
    if not perms.role_allows(m.role, capability):
        needed = perms.MINIMUM_ROLE[capability]
        raise HttpError(403, f"this requires the {needed} role or above")
    return m


def _require_may_manage(m: WorkspaceMembership, target_role: str | None,
                        new_role: str | None = None) -> None:
    """Below owner, members are managed strictly beneath yourself: an admin
    invites, re-roles and removes viewers and editors, never an admin or an
    owner, and never grants admin or owner (`permissions.may_manage_member`)."""
    if not perms.may_manage_member(m.role, target_role, new_role):
        raise HttpError(403, "you can only manage members, and grant roles, below your own")


def _target_role(slug: str, user_id: int) -> str | None:
    """The role of the member being acted on, through the one authorizer —
    a question about someone else, so it is asked of them."""
    from django.contrib.auth import get_user_model

    target = get_user_model().objects.filter(pk=user_id).first()
    return services.member_role(target, slug) if target is not None else None


def _member_out(m: WorkspaceMembership) -> MemberOut:
    return MemberOut(user_id=m.user_id, email=m.user.email, role=m.role, joined_at=m.joined_at,
                     inherited=getattr(m, "inherited", False))


def _invite_out(inv: WorkspaceInvite, email_status: str | None = None, *,
                with_token: bool = True) -> InviteOut:
    return InviteOut(
        id=inv.id, email=inv.email, role=inv.role, token=inv.token if with_token else "",
        expires_at=inv.expires_at, accepted_at=inv.accepted_at, revoked_at=inv.revoked_at,
        created_at=inv.created_at, invited_by_email=inv.invited_by.email or None,
        last_emailed_at=inv.last_emailed_at, email_status=email_status,
    )


@router.post("/", response={201: WorkspaceOut}, summary="Create a workspace",)
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
    if payload.parent:
        # Owner of the parent only: every owner of the parent becomes an owner
        # of the child, so nesting is the parent's administrators' call.
        _require(request.user, payload.parent, perms.OWN)
    ws = Workspace.objects.create(
        slug=payload.slug,
        display_name=payload.display_name,
        parent_id=payload.parent or None,
        created_by=request.user,
        # access_request_domains is deliberately NOT settable from the
        # request — see WorkspaceCreateIn. Only `ensure_default_workspace()`
        # and migrations set it; auto-approval starts off.
    )
    WorkspaceMembership.objects.create(
        workspace=ws, user=request.user, role=WorkspaceMembership.OWNER
    )
    return Status(201, _out(ws, WorkspaceMembership.OWNER))


@router.get("/", response=list[WorkspaceOut], summary="List my workspaces",)
def list_workspaces(request: HttpRequest) -> list[WorkspaceOut]:
    # Direct memberships PLUS workspaces owned by inheritance (descendants of
    # one the caller owns) — read through `services`, the sole authorizer.
    #
    # ORDER IS A CONTRACT: the client's default workspace — where `/` and every
    # legacy flat route (`/agents`, …) land — is the FIRST entry. Before the
    # tree it was "your newest direct membership", and inherited rows must not
    # displace that: sorting everything by `created_at` put four divisions
    # created minutes earlier (all empty) ahead of `connect`, so the Agents
    # page opened on a workspace with no agents. Direct memberships first
    # (newest first, as before), then inherited ones.
    slugs = services.user_workspace_slugs(request.user)
    rows = []
    for ws in Workspace.objects.filter(slug__in=slugs).order_by("-created_at"):
        m = services.membership(request.user, ws)
        if m is not None:
            rows.append(_m_out(m))
    return sorted(rows, key=lambda w: w.inherited)  # stable: keeps -created_at within each group


@router.get("/{slug}/", response=WorkspaceOut, summary="Get a workspace (member-only)",)
def get_workspace(request: HttpRequest, slug: str) -> WorkspaceOut:
    m = _membership_or_404(request.user, slug)
    return _m_out(m)


@router.put("/{slug}/parent", response=WorkspaceOut, summary="Move a workspace in the tree (owner-only)",)
def set_workspace_parent(request: HttpRequest, slug: str, payload: WorkspaceParentIn) -> WorkspaceOut:
    """Nest `slug` under `parent`, or make it a root with `parent: null`.

    Owner of BOTH ends: of the workspace being moved (it changes who
    administers it) and of the new parent (its owners gain this workspace).
    A cycle is refused by `Workspace.save` and surfaces as 422."""
    m = _require(request.user, slug, perms.OWN)
    if payload.parent:
        _require(request.user, payload.parent, perms.OWN)
    ws = m.workspace
    if ws.parent_id and not payload.parent:
        # Detaching to a root ends every inherited ownership of it. Done by
        # someone who only owned it by inheritance, it used to save and then
        # 500 (they were no longer a member to describe it to) — and it left
        # a workspace nobody owned. So: only a DIRECT owner may detach, and
        # never into a workspace with no direct owner left to run it.
        if getattr(m, "inherited", False):
            raise HttpError(409, "only a direct owner of this workspace can make it a root; "
                                 "you own it through its parent, and would lose it")
    from django.core.exceptions import ValidationError
    from django.db import transaction

    try:
        with transaction.atomic():
            ws.parent_id = payload.parent or None
            ws.save()
            mine = services.membership(request.user, ws)
            if mine is None:
                # Cannot happen given the checks above (a direct owner stays one,
                # and the new parent is one the caller owns) — but if it ever
                # did, the move must not land with nobody to describe it to.
                raise HttpError(409, "this move would leave you outside the workspace")
    except ValidationError as exc:
        raise HttpError(422, "; ".join(exc.messages))
    return _m_out(mine)


# ---- access requests ("request an invitation") ----
_ACCESS_ERROR = {
    "not_found": (404, None),
    "already_member": (409, "you are already a member of this workspace"),
    "not_pending": (409, "this request has already been decided"),
    "invalid_role": (422, "a request can be approved as viewer, editor or admin"),
}


def _access_error(exc: "services.AccessRequestError", slug: str) -> HttpError:
    status, message = _ACCESS_ERROR[exc.code]
    return HttpError(status, message or f"workspace '{slug}' not found")


def _access_request_out(req: WorkspaceAccessRequest, *, admin_view: bool) -> AccessRequestOut:
    user = req.user
    return AccessRequestOut(
        id=req.pk,
        workspace=req.workspace_id,
        workspace_display_name=req.workspace.display_name,
        user_id=req.user_id,
        email=user.email or "",
        name=(user.get_full_name() or "").strip(),
        note=req.note,
        status=req.status,
        role=req.role,
        auto=req.auto,
        decided_by_email=(req.decided_by.email or None) if req.decided_by_id else None,
        decided_at=req.decided_at,
        decision_reason=req.decision_reason,
        created_at=req.created_at,
        current_role=services.member_role(user, req.workspace_id),
        notify_result=req.notify_result if admin_view else None,
    )


def _access_request_or_404(m: WorkspaceMembership, request_id: int) -> WorkspaceAccessRequest:
    req = (WorkspaceAccessRequest.objects.select_related("workspace", "user", "decided_by")
           .filter(workspace=m.workspace, pk=request_id).first())
    if req is None:
        raise HttpError(404, "access request not found")
    return req


@router.get("/requestable", response=list[RequestableWorkspaceOut],
            summary="Workspaces I may request an invitation to")
def list_requestable_workspaces(request: HttpRequest) -> list[RequestableWorkspaceOut]:
    """A capability list, not a directory: only workspaces whose
    `access_request_domains` include the caller's login-email domain and that
    they are not already in, each with their own open request if any. Joins
    nothing — see `services.requestable_workspaces`."""
    return [
        RequestableWorkspaceOut(slug=ws.slug, display_name=ws.display_name, domain=domain,
                                pending_request_id=pending.pk if pending else None)
        for ws, domain, pending in services.requestable_workspaces(request.user)
    ]


@router.post("/{slug}/access-requests", response={200: AccessRequestOut, 201: AccessRequestOut},
             summary="Request an invitation to a workspace")
def request_workspace_access(request: HttpRequest, slug: str, payload: AccessRequestIn) -> Status:
    """Ask this workspace's admins to let you in, with an optional note. Every
    admin and owner is emailed a link to the request. With the workspace's
    `auto_approve_role` set you are in at once at that role (`status:
    approved`); otherwise the request is `pending` until an admin decides.
    Idempotent while pending (200 with the open request). A workspace that
    does not exist and one whose domains do not include yours are the SAME
    404, so this cannot probe tenants; 409 if you are already a member."""
    try:
        req, created = services.request_access(request.user, slug, payload.note)
    except services.AccessRequestError as exc:
        raise _access_error(exc, slug) from None
    return Status(201 if created else 200, _access_request_out(req, admin_view=False))


@router.get("/{slug}/access-requests", response=list[AccessRequestOut],
            summary="List access requests (admin or owner)")
def list_access_requests(request: HttpRequest, slug: str, status: str | None = None) -> list[AccessRequestOut]:
    """Newest first; `status` narrows to pending / approved / denied."""
    m = _require(request.user, slug, perms.MEMBERS_MANAGE)
    qs = (WorkspaceAccessRequest.objects.filter(workspace=m.workspace)
          .select_related("workspace", "user", "decided_by").order_by("-created_at"))
    if status:
        qs = qs.filter(status=status)
    return [_access_request_out(r, admin_view=True) for r in qs[:500]]


@router.get("/{slug}/access-requests/{request_id}", response=AccessRequestOut,
            summary="One access request (admin or owner)")
def get_access_request(request: HttpRequest, slug: str, request_id: int) -> AccessRequestOut:
    m = _require(request.user, slug, perms.MEMBERS_MANAGE)
    return _access_request_out(_access_request_or_404(m, request_id), admin_view=True)


@router.post("/{slug}/access-requests/{request_id}/approve", response=AccessRequestOut,
             summary="Approve an access request at a role (admin or owner)")
def approve_access_request(request: HttpRequest, slug: str, request_id: int,
                           payload: AccessRequestApproveIn) -> AccessRequestOut:
    """Creates the membership at `role` (default viewer) and emails the
    requester. You may grant only a role below your own unless you are an
    owner (`permissions.may_manage_member`) — 403 otherwise. 409 if the
    request was already decided."""
    m = _require(request.user, slug, perms.MEMBERS_MANAGE)
    _require_may_manage(m, None, payload.role)
    req = _access_request_or_404(m, request_id)
    try:
        req = services.approve_access_request(request=req, by=request.user, role=payload.role)
    except services.AccessRequestError as exc:
        raise _access_error(exc, slug) from None
    return _access_request_out(req, admin_view=True)


@router.post("/{slug}/access-requests/{request_id}/deny", response=AccessRequestOut,
             summary="Deny an access request (admin or owner)")
def deny_access_request(request: HttpRequest, slug: str, request_id: int,
                        payload: AccessRequestDenyIn) -> AccessRequestOut:
    """Grants nothing; emails the requester, with `reason` if given. 409 if the
    request was already decided."""
    m = _require(request.user, slug, perms.MEMBERS_MANAGE)
    req = _access_request_or_404(m, request_id)
    try:
        req = services.deny_access_request(request=req, by=request.user, reason=payload.reason)
    except services.AccessRequestError as exc:
        raise _access_error(exc, slug) from None
    return _access_request_out(req, admin_view=True)


@router.put("/{slug}/access-settings", response=WorkspaceOut,
            summary="Set how access requests are approved (owner-only)")
def set_access_settings(request: HttpRequest, slug: str, payload: AccessSettingsIn) -> WorkspaceOut:
    """`auto_approve_role`: "" (off — a person approves each request),
    "viewer" or "editor". Owner-only: it decides who gets in without anyone
    looking. Takes effect on the next request; nobody already in changes."""
    m = _require(request.user, slug, perms.OWN)
    ws = m.workspace
    ws.auto_approve_role = payload.auto_approve_role
    ws.save(update_fields=["auto_approve_role", "updated_at"])
    return _m_out(m)


@router.delete("/{slug}/", response={204: None}, summary="Delete a workspace (owner-only)",)
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
    _require(request.user, slug, perms.OWN)
    ws = Workspace.objects.filter(slug=slug).first()
    if ws is None:
        raise HttpError(404, f"workspace '{slug}' not found")
    child_slugs = sorted(ws.children.values_list("slug", flat=True))
    if child_slugs:
        raise HttpError(
            409,
            f"workspace '{slug}' has {len(child_slugs)} child workspace(s): "
            f"{', '.join(child_slugs)}. Delete or move them first.",
        )
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
@router.get("/{slug}/members/", response=list[MemberOut], summary="List members (member-only)",)
def list_members(request: HttpRequest, slug: str) -> list[MemberOut]:
    """Everyone in the workspace. Owners of a parent workspace own this one
    too, and are listed with `inherited: true`; they are changed on the parent."""
    m = _membership_or_404(request.user, slug)
    return [_member_out(row) for row in services.effective_memberships(m.workspace)]


@router.delete("/{slug}/members/{user_id}/", response={204: None},
               summary="Remove a member (admin or owner)")
def remove_member(request: HttpRequest, slug: str, user_id: int):
    m = _require(request.user, slug, perms.MEMBERS_MANAGE)
    _require_may_manage(m, _target_role(slug, user_id))
    try:
        services.remove_member(workspace=m.workspace, user_id=user_id, by=request.user)
    except services.MemberError as exc:
        raise HttpError(_MEMBER_ERROR_STATUS[exc.code], _REMOVE_MEMBER_ERROR_MESSAGES[exc.code])
    return Status(204, None)


@router.patch("/{slug}/members/{user_id}/", response=MemberOut,
              summary="Change a member's role (admin or owner)")
def set_member_role(request: HttpRequest, slug: str, user_id: int, payload: MemberRoleUpdateIn) -> MemberOut:
    m = _require(request.user, slug, perms.MEMBERS_MANAGE)
    _require_may_manage(m, _target_role(slug, user_id), payload.role)
    try:
        updated = services.set_member_role(workspace=m.workspace, user_id=user_id, role=payload.role,
                                           by=request.user)
    except services.MemberError as exc:
        raise HttpError(_MEMBER_ERROR_STATUS[exc.code], _SET_MEMBER_ROLE_ERROR_MESSAGES[exc.code])
    return _member_out(updated)


# ---- invites ----
@router.post("/{slug}/invites/", response={201: InviteOut}, summary="Invite by email (admin or owner)",)
def create_invite(request: HttpRequest, slug: str, payload: InviteCreateIn) -> Status:
    """Creates the invite and emails its link to the address. `email_status`
    says whether the email went out; the link in `token` works either way.
    Inviting an address that already has an outstanding invite returns that
    invite and emails its link again (at most once a minute)."""
    m = _require(request.user, slug, perms.MEMBERS_MANAGE)
    _require_may_manage(m, None, payload.role)
    inv = services.create_invite(
        workspace=m.workspace, email=payload.email, role=payload.role, invited_by=request.user,
    )
    return Status(201, _invite_out(inv, services.email_invite(invite=inv)))


@router.get("/{slug}/invites/", response=list[InviteOut], summary="List invites (member-only)",)
def list_invites(request: HttpRequest, slug: str) -> list[InviteOut]:
    """Every member sees who has been invited; only an owner gets each invite's
    `token` (it is empty for everyone else)."""
    m = _membership_or_404(request.user, slug)
    # A pending invite's token IS the invite — the link that admits someone at
    # its role, owner included — and handing it out is an owner's act, like
    # creating it. Every member used to receive every token, so a viewer could
    # forward an owner-level link the owners never meant to send. A viewer
    # still sees the list (the page shows it to everyone), just not the links.
    return [
        _invite_out(i, with_token=perms.role_allows(m.role, perms.MEMBERS_MANAGE)
                    and perms.may_manage_member(m.role, None, i.role))
        for i in WorkspaceInvite.objects.filter(workspace_id=slug)
        .select_related("invited_by")
        .order_by("-created_at")
    ]


@router.post("/{slug}/invites/{invite_id}/revoke", response={204: None},
             summary="Revoke an invite (admin or owner)")
def revoke_invite(request: HttpRequest, slug: str, invite_id: int):
    m = _require(request.user, slug, perms.MEMBERS_MANAGE)
    try:
        inv = WorkspaceInvite.objects.get(workspace_id=slug, id=invite_id)
    except WorkspaceInvite.DoesNotExist:
        raise HttpError(404, "invite not found")
    _require_may_manage(m, None, inv.role)
    services.revoke_invite(invite=inv)
    return Status(204, None)


@router.post("/{slug}/invites/{invite_id}/reissue", response=InviteOut,
             summary="Send a fresh link for an invite (admin or owner)")
def reissue_invite(request: HttpRequest, slug: str, invite_id: int) -> InviteOut:
    """New token and a fresh expiry for an invite nobody has accepted or
    revoked — including one that has expired — emailed to the invited address.
    The previous link stops working. Accepted or revoked invites answer 410;
    invite the address again instead. 429 if this invite was emailed under a
    minute ago."""
    m = _require(request.user, slug, perms.MEMBERS_MANAGE)
    try:
        inv = WorkspaceInvite.objects.select_related("invited_by", "workspace").get(
            workspace_id=slug, id=invite_id
        )
    except WorkspaceInvite.DoesNotExist:
        raise HttpError(404, "invite not found")
    _require_may_manage(m, None, inv.role)
    # Checked BEFORE rotating: rotating and then throttling the email would
    # kill the link the person already has without sending them the new one.
    # A finished invite falls through to the service's 410 instead.
    outstanding = inv.accepted_at is None and inv.revoked_at is None
    if outstanding and services.recently_emailed(inv):
        raise HttpError(429, "this invite was just emailed — wait a minute before sending another")
    try:
        inv = services.reissue_invite(invite=inv)
    except services.InviteError as exc:
        raise HttpError(_INVITE_ERROR_STATUS[exc.code], f"invite {exc.code.replace('_', ' ')}")
    return _invite_out(inv, services.email_invite(invite=inv))


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
             summary="Accept an invite by token")
def accept_invite(request: HttpRequest, token: str) -> WorkspaceOut:
    try:
        ws, role = services.accept_invite(token=token, user=request.user)
    except services.InviteError as exc:
        status = _INVITE_ERROR_STATUS[exc.code]
        raise HttpError(status, exc.code)
    return _out(ws, role)


@router.get("/{slug}/shared-vault", response=SharedVaultOut,
            summary="This tenant's shared 1Password vault (masked — never the key)")
def get_shared_vault(request: HttpRequest, slug: str) -> SharedVaultOut:
    """Owner-only, matching the rest of workspace administration.

    Reading it is masked, so the restriction is not about the value — it is that
    which vault a tenant reads is administrative, and an editor acts *within* a
    tenant rather than over its credential configuration.
    """
    m = _require(request.user, slug, perms.OWN)
    return services.shared_vault_status(m.workspace)


@router.put("/{slug}/shared-vault", response=SharedVaultOut,
            summary="Set the shared vault + its service-account token (write-only)")
def set_shared_vault(request: HttpRequest, slug: str, payload: SharedVaultIn) -> SharedVaultOut:
    """The key here must be scoped to the SHARED vault and nothing else.

    A key that also reads the per-agent vaults would undo the reason those are
    split (Agent.op_vault, 2026-09-06): what this one unlocks is shared by
    definition, so its breadth costs nothing, and that is only true while it
    stays narrow. Nothing here can enforce that — 1Password grants it — so it is
    stated where whoever sets it will read it.
    """
    m = _require(request.user, slug, perms.OWN)
    return services.set_shared_vault(
        m.workspace, vault=payload.vault, service_key=payload.service_key,
    )


@router.get("/{slug}/runner-topology", response=RunnerTopologyOut,
            summary="Which runners serve which agents, across this workspace and every one below it")
def runner_topology(request: HttpRequest, slug: str) -> RunnerTopologyOut:
    """This workspace and its descendants, each with its agents and their routing
    (the default ordered list and every source rule), plus every runner those
    agents route to or that lives in the tree. Admin and above."""
    # `logs.read` — the operational read tier (runner drills, health) — on the
    # root, and again on every descendant: only OWNERSHIP flows down the tree,
    # so an admin of the root sees a division only where they are its admin too.
    from apps.harness import topology

    m = _require(request.user, slug, perms.LOGS_READ)
    return RunnerTopologyOut(**topology.build(
        m.workspace, visible=lambda s: perms.can(request.user, s, perms.LOGS_READ),
    ))


@router.get("/{slug}/runner-order", response=list[RunnerOrderRowOut],
            summary="The workspace's default runner order: repo turns, and agents without their own")
def get_runner_order(request: HttpRequest, slug: str) -> list[RunnerOrderRowOut]:
    """Repo turns (a project dispatch — no agent) route by this list: rank 0 takes
    the turn while it is available, the next rank only once every better one is
    not (or the turn has waited past the cascade grace), and a runner not listed
    never takes one. Empty: any runner that declares the repo.

    It is also the default order of every AGENT here that has none of its own,
    and of every workspace below this one that has no order of its own. Such an
    agent's source rules still come first. Member-readable, like an agent's own
    order."""
    from apps.harness.models import WorkspaceRunnerOrder

    m = _require(request.user, slug, perms.READ)
    rows = (WorkspaceRunnerOrder.objects.filter(workspace=m.workspace)
            .select_related("runner").order_by("rank"))
    return [
        RunnerOrderRowOut(
            runner_id=row.runner_id, runner_name=row.runner.name, kind=row.runner.kind,
            rank=row.rank, online=row.runner.live_status == row.runner.ONLINE,
            ready=row.runner.ready, enabled=row.enabled,
            projects=row.runner.project_names(),
        )
        for row in rows
    ]


@router.put("/{slug}/runner-order", response=list[RunnerOrderRowOut],
            summary="Replace the workspace's default runner order")
def set_runner_order(request: HttpRequest, slug: str, payload: RunnerOrderIn) -> list[RunnerOrderRowOut]:
    """Wholesale replace (index = rank). Workspace admins and owners
    (`runners.route`): the order routes agents in every workspace below this one
    too. Every runner must be able to SERVE this workspace —
    its owner a member (`runner_tenant_slugs`) — or the order would name a box the
    claim path refuses anyway; such a runner is a 422, as is an unknown or retired
    one."""
    # Maintainer note: was `agent.work` while the order routed only this
    # workspace's repo turns; an agent following it may live in a division where
    # an editor here holds no role at all. Then `own`; lowered to admin
    # (2026-10-05) — the runners it names must already serve this workspace.
    from django.db import transaction

    from apps.harness.models import Runner, WorkspaceRunnerOrder
    from apps.harness.services import runner_tenant_slugs

    m = _require(request.user, slug, perms.RUNNERS_ROUTE)
    ids = [row.runner_id for row in payload.runners]
    if len(ids) != len(set(ids)):
        raise HttpError(422, "duplicate runner id in list")
    runners = {r.id: r for r in Runner.objects.filter(id__in=ids)
               .exclude(status=Runner.RETIRED).select_related("owner")}
    missing = [str(rid) for rid in ids if rid not in runners]
    if missing:
        raise HttpError(422, f"unknown or retired runner id(s): {', '.join(missing)}")
    outside = sorted(r.name for r in runners.values() if slug not in runner_tenant_slugs(r))
    if outside:
        raise HttpError(422, f"runner(s) {', '.join(outside)} cannot serve '{slug}': "
                             "a runner's owner must be a member of the workspace")
    with transaction.atomic():
        WorkspaceRunnerOrder.objects.filter(workspace=m.workspace).delete()
        WorkspaceRunnerOrder.objects.bulk_create([
            WorkspaceRunnerOrder(workspace=m.workspace, runner=runners[row.runner_id],
                                 rank=i, enabled=row.enabled)
            for i, row in enumerate(payload.runners)
        ])
    return get_runner_order(request, slug)


@router.get("/{slug}/agent-topology", response=AgentTopologyOut,
            summary="Which agents can send which other agents work, across this workspace and every one below it")
def agent_topology(request: HttpRequest, slug: str) -> AgentTopologyOut:
    """Every agent in this workspace and its descendants, and for each ordered
    pair what the first gets when it sends the second work with its own canopy
    login: the whole agent, some of its capabilities, or nothing — and why.
    Admin and above. Granting access is `PUT /api/agents/{slug}/admins/{user_id}`
    with the source agent's `login_user_id`."""
    # Same gate and per-descendant filter as runner_topology.
    from apps.agents import topology

    m = _require(request.user, slug, perms.LOGS_READ)
    return AgentTopologyOut(**topology.build(
        m.workspace, request.user,
        visible=lambda s: perms.can(request.user, s, perms.LOGS_READ),
    ))

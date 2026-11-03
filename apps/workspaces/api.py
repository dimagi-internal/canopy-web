"""Django Ninja router for /api/workspaces — the multi-tenancy surface.

Membership-scoped: a workspace is visible only to its members; a non-member
gets 404 (no existence leak). Creating a workspace makes the creator its owner.
Admins and owners manage members + invites (`_require` + `permissions`); invites are accepted
by token, only by the addressed email. People whose login email is at one of a
workspace's `access_request_domains` may REQUEST an invitation; an admin or owner
approves or denies it (docs/architecture/access.md, "Getting into a workspace").
"""
from __future__ import annotations

from collections import Counter

from django.db import transaction
from django.db.models import ProtectedError
from django.http import HttpRequest
from ninja import Router, Status
from ninja.errors import HttpError

from apps.api.auth import session_auth
from apps.api.route_gates import gate
from apps.workspaces import permissions as perms

from . import access_requests, invites, login_gate, notifications, services
from .gates import membership_or_404 as _membership_or_404
from .gates import require as _require
from .gates import tree_reader as _tree_reader
from .models import (
    Workspace,
    WorkspaceAccessRequest,
    WorkspaceInvite,
    WorkspaceMembership,
)
from .schemas import (
    AccessRequestApproveIn,
    AccessRequestDenyIn,
    AccessRequestIn,
    AccessRequestOut,
    AccessSettingsIn,
    InviteCreateIn,
    InviteOut,
    InvitePreviewOut,
    MemberAddIn,
    MemberOut,
    MemberRoleUpdateIn,
    MemberTreeCellOut,
    MemberTreeOut,
    MemberTreePersonOut,
    MemberTreeWorkspaceOut,
    MyInviteOut,
    RequestableWorkspaceOut,
    WorkspaceAnalyticsOut,
    WorkspaceCreateIn,
    WorkspaceNetworkOut,
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
_ADD_MEMBER_ERROR_MESSAGES = {
    "not_found": "no such person in this workspace tree",
    "already_member": "already a member here; change their role instead",
    "invalid_role": "unknown role",
}
_ADD_MEMBER_ERROR_STATUS = {**_MEMBER_ERROR_STATUS, "already_member": 409}
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
        root=services.tree_roots({ws.slug})[ws.slug],
        inherited=inherited,
    )


def _m_out(m: WorkspaceMembership) -> WorkspaceOut:
    return _out(m.workspace, m.role, inherited=getattr(m, "inherited", False))


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
@gate("authenticated", "own", setting=True)  # can_create_workspace; OWN on `parent` when nesting
def create_workspace(request: HttpRequest, payload: WorkspaceCreateIn) -> Status:
    # An invite-admitted user (cleared the OAuth gate via a live invite or an
    # existing membership, not the domain allowlist) must not be able to
    # bootstrap their own workspace and mint invites of their own — that
    # would make invite-admission transitively delegable to an attacker. See
    # `login_gate.can_create_workspace` + the F1 security finding.
    if not login_gate.can_create_workspace(request.user):
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
    if not ws.parent_id:
        # A root created by someone an org counts as its own: tell that org's
        # owners (best-effort, never fails the create).
        notifications.notify_root_created(ws, request.user)
    return Status(201, _out(ws, WorkspaceMembership.OWNER))


@router.get("/", response=list[WorkspaceOut], summary="List my workspaces",)
@gate("authenticated")  # the caller's own memberships
def list_workspaces(request: HttpRequest) -> list[WorkspaceOut]:
    # Direct memberships PLUS workspaces held by inheritance (descendants of
    # one the caller owns or administers) — read through `services`, the sole authorizer.
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


@router.get("/invites/mine", response=list[MyInviteOut], summary="Invites waiting for me")
@gate("authenticated", step_up="the caller's own invite links, already emailed to them")
def my_invites(request: HttpRequest) -> list[MyInviteOut]:
    """Invites addressed to you that you have not accepted yet, newest first.
    Open `/invite/<token>` to accept one."""
    # Matched on the signed-in email's mailbox (`invites.mailbox_key`), so a Gmail
    # invite sent to another dot spelling still shows. Listing joins nothing:
    # only accepting does.
    return [
        MyInviteOut(
            token=inv.token, workspace_slug=inv.workspace.slug,
            workspace_display_name=inv.workspace.display_name, role=inv.role,
            invited_by_email=inv.invited_by.email or None, expires_at=inv.expires_at,
        )
        for inv in invites.pending_invites_for(request.user.email)
    ]


@router.get("/{slug}/", response=WorkspaceOut, summary="Get a workspace (member-only)",)
@gate("member")
def get_workspace(request: HttpRequest, slug: str) -> WorkspaceOut:
    m = _membership_or_404(request.user, slug)
    return _m_out(m)


@router.put("/{slug}/parent", response=WorkspaceOut, summary="Move a workspace in the tree (owner-only)",)
@gate("own", setting=True)  # own both ends
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
    # Moving it under ANOTHER parent ends the old parent's inherited ownerships
    # just as detaching does: an inherited owner re-parenting it under a root
    # only they own cut every co-owner out. So an inherited owner may move it
    # only where every current owner stays one (an org owner moving a division
    # between two orgs they both run); a direct owner keeps the say they have.
    owners_before = (
        services.owner_user_ids(ws)
        if ws.parent_id and payload.parent and payload.parent != ws.parent_id
        and getattr(m, "inherited", False) else None
    )
    from django.core.exceptions import ValidationError
    from django.db import transaction

    try:
        with transaction.atomic():
            ws.parent_id = payload.parent or None
            ws.save()
            if owners_before is not None and not owners_before <= services.owner_user_ids(ws):
                raise HttpError(409, "only a direct owner of this workspace can move it where "
                                     "its current owners would lose it; you own it through "
                                     "its parent")
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


def _access_error(exc: "access_requests.AccessRequestError", slug: str) -> HttpError:
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
@gate("authenticated")  # capability list: access_request_domains match
def list_requestable_workspaces(request: HttpRequest) -> list[RequestableWorkspaceOut]:
    """A capability list, not a directory: only workspaces whose
    `access_request_domains` include the caller's login-email domain and that
    they are not already in, each with their own open request if any. Joins
    nothing — see `services.requestable_workspaces`."""
    # It lives in `access_requests.requestable_workspaces`.
    return [
        RequestableWorkspaceOut(slug=ws.slug, display_name=ws.display_name, domain=domain,
                                pending_request_id=pending.pk if pending else None)
        for ws, domain, pending in access_requests.requestable_workspaces(request.user)
    ]


@router.post("/{slug}/access-requests", response={200: AccessRequestOut, 201: AccessRequestOut},
             summary="Request an invitation to a workspace")
# access_request_domains match, same 404 otherwise; joins nothing unless auto_approve_role
@gate("authenticated", kind="request")
def request_workspace_access(request: HttpRequest, slug: str, payload: AccessRequestIn) -> Status:
    """Ask this workspace's admins to let you in, with an optional note. Every
    admin and owner is emailed a link to the request. With the workspace's
    `auto_approve_role` set you are in at once at that role (`status:
    approved`); otherwise the request is `pending` until an admin decides.
    Idempotent while pending (200 with the open request). A workspace that
    does not exist and one whose domains do not include yours are the SAME
    404, so this cannot probe tenants; 409 if you are already a member."""
    try:
        req, created = access_requests.request_access(request.user, slug, payload.note)
    except access_requests.AccessRequestError as exc:
        raise _access_error(exc, slug) from None
    return Status(201 if created else 200, _access_request_out(req, admin_view=False))


@router.get("/{slug}/access-requests", response=list[AccessRequestOut],
            summary="List access requests (admin or owner)")
@gate("members.manage")
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
@gate("members.manage")
def get_access_request(request: HttpRequest, slug: str, request_id: int) -> AccessRequestOut:
    m = _require(request.user, slug, perms.MEMBERS_MANAGE)
    return _access_request_out(_access_request_or_404(m, request_id), admin_view=True)


@router.post("/{slug}/access-requests/{request_id}/approve", response=AccessRequestOut,
             summary="Approve an access request at a role (admin or owner)")
@gate("members.manage", setting=True)  # + may_manage_member on the granted role
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
        req = access_requests.approve_access_request(request=req, by=request.user, role=payload.role)
    except access_requests.AccessRequestError as exc:
        raise _access_error(exc, slug) from None
    return _access_request_out(req, admin_view=True)


@router.post("/{slug}/access-requests/{request_id}/deny", response=AccessRequestOut,
             summary="Deny an access request (admin or owner)")
@gate("members.manage", setting=True)
def deny_access_request(request: HttpRequest, slug: str, request_id: int,
                        payload: AccessRequestDenyIn) -> AccessRequestOut:
    """Grants nothing; emails the requester, with `reason` if given. 409 if the
    request was already decided."""
    m = _require(request.user, slug, perms.MEMBERS_MANAGE)
    req = _access_request_or_404(m, request_id)
    try:
        req = access_requests.deny_access_request(request=req, by=request.user, reason=payload.reason)
    except access_requests.AccessRequestError as exc:
        raise _access_error(exc, slug) from None
    return _access_request_out(req, admin_view=True)


@router.put("/{slug}/access-settings", response=WorkspaceOut,
            summary="Set how access requests are approved (owner-only)")
@gate("own", setting=True)
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
@gate("own", setting=True)
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
    - **Refuses while it holds anything else that keeps a workspace** — a
      project turn's history, a chat session — with a 409 counting what is there.
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
    # Agents are the common blocker, named above; anything else that keeps a
    # tenant (a project turn's history, a chat session) is PROTECT too. Name it
    # rather than let ProtectedError surface as a 500.
    try:
        with transaction.atomic():
            ws.delete()
    except ProtectedError as e:
        held = Counter(obj._meta.verbose_name_plural for obj in e.protected_objects)
        # A model whose rows must be removed through its own app (which may need to
        # tell a provider, say) names how, as `workspace_delete_hint`: core cannot
        # import the framework apps that own them.
        hints = sorted({h for obj in e.protected_objects if (h := getattr(obj, "workspace_delete_hint", ""))})
        raise HttpError(
            409,
            f"workspace '{slug}' still holds "
            + ", ".join(f"{n} {what}" for what, n in sorted(held.items()))
            + ". It is not empty, so it cannot be deleted."
            + "".join(f" {h}" for h in hints),
        ) from None
    return Status(204, None)


# ---- members ----
@router.get("/{slug}/members/", response=list[MemberOut], summary="List members (member-only)",)
@gate("member")
def list_members(request: HttpRequest, slug: str) -> list[MemberOut]:
    """Everyone in the workspace. Owners of a parent workspace own this one
    too, and are listed with `inherited: true`; they are changed on the parent."""
    m = _membership_or_404(request.user, slug)
    return [_member_out(row) for row in services.effective_memberships(m.workspace)]


@router.post("/{slug}/members/", response={201: MemberOut},
             summary="Add someone already in this workspace tree (admin or owner)")
@gate("members.manage", setting=True)  # + may_manage_member on the granted role
def add_member(request: HttpRequest, slug: str, payload: MemberAddIn) -> Status:
    """Adds a person who is already a member of another workspace in this tree
    (one you are in too) directly, at `role` — no invite, no email. 404 for
    anyone else, so this never adds an arbitrary user id; 409 if they already
    hold a role here (change it with PATCH); 403 for a role you may not grant."""
    m = _require(request.user, slug, perms.MEMBERS_MANAGE)
    _require_may_manage(m, None, payload.role)
    try:
        added = services.add_tree_member(
            workspace=m.workspace, user_id=payload.user_id, role=payload.role,
            visible=set(services.member_roles(request.user)), by=request.user,
        )
    except services.MemberError as exc:
        raise HttpError(_ADD_MEMBER_ERROR_STATUS[exc.code], _ADD_MEMBER_ERROR_MESSAGES[exc.code])
    return Status(201, _member_out(added))


@router.get("/{slug}/member-tree", response=MemberTreeOut,
            summary="Everyone across this workspace and the ones below it (member-only)")
@gate("member")
def member_tree(request: HttpRequest, slug: str) -> MemberTreeOut:
    """This workspace and every workspace below it that you are in, in tree
    order, and one row per person who is in any of them with their role in
    each (`cells`; a missing slug = not a member there). Owners and admins of
    an ancestor are in every workspace below it by inheritance
    (`inherited: true`, `inherited_from` the ancestor); editors and viewers are
    not, which is the gap this view makes visible. `can_manage` says where you
    may add, re-role or remove people."""
    m = _membership_or_404(request.user, slug)
    # Read every role once (`member_roles`), not `membership()` per workspace.
    mine = services.member_roles(request.user)
    tree = services.subtree(m.workspace, visible=lambda s: s in mine)
    workspaces: list[MemberTreeWorkspaceOut] = []
    people: dict[int, MemberTreePersonOut] = {}
    for ws, depth in tree:
        my_role = mine.get(ws.slug) or m.role
        workspaces.append(MemberTreeWorkspaceOut(
            slug=ws.slug, display_name=ws.display_name, parent=ws.parent_id, depth=depth,
            my_role=my_role, can_manage=perms.role_allows(my_role, perms.MEMBERS_MANAGE),
        ))
        for row in services.effective_memberships(ws):
            person = people.get(row.user_id)
            if person is None:
                person = people[row.user_id] = MemberTreePersonOut(
                    user_id=row.user_id, email=row.user.email, cells={},
                )
            inherited = getattr(row, "inherited", False)
            person.cells[ws.slug] = MemberTreeCellOut(
                role=row.role, inherited=inherited,
                inherited_from=getattr(row, "inherited_from", None) if inherited else None,
            )
    return MemberTreeOut(
        root=m.workspace.slug, workspaces=workspaces,
        people=sorted(people.values(), key=lambda p: p.email.lower()),
    )


@router.delete("/{slug}/members/{user_id}/", response={204: None},
               summary="Remove a member (admin or owner)")
@gate("members.manage", setting=True)  # + may_manage_member
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
@gate("members.manage", setting=True)  # + may_manage_member
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
@gate("members.manage", setting=True, step_up="an invite link")
def create_invite(request: HttpRequest, slug: str, payload: InviteCreateIn) -> Status:
    """Creates the invite and emails its link to the address. `email_status`
    says whether the email went out; the link in `token` works either way.
    Inviting an address that already has an outstanding invite returns that
    invite and emails its link again (at most once a minute)."""
    m = _require(request.user, slug, perms.MEMBERS_MANAGE)
    _require_may_manage(m, None, payload.role)
    inv = invites.create_invite(
        workspace=m.workspace, email=payload.email, role=payload.role, invited_by=request.user,
    )
    return Status(201, _invite_out(inv, notifications.email_invite(invite=inv)))


@router.get("/{slug}/invites/", response=list[InviteOut], summary="List invites (member-only)",)
@gate("member", step_up="an invite link")  # tokens only to members.manage
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
@gate("members.manage", setting=True)
def revoke_invite(request: HttpRequest, slug: str, invite_id: int):
    m = _require(request.user, slug, perms.MEMBERS_MANAGE)
    try:
        inv = WorkspaceInvite.objects.get(workspace_id=slug, id=invite_id)
    except WorkspaceInvite.DoesNotExist:
        raise HttpError(404, "invite not found")
    _require_may_manage(m, None, inv.role)
    invites.revoke_invite(invite=inv)
    return Status(204, None)


@router.post("/{slug}/invites/{invite_id}/reissue", response=InviteOut,
             summary="Send a fresh link for an invite (admin or owner)")
@gate("members.manage", setting=True, step_up="an invite link")
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
    if outstanding and notifications.recently_emailed(inv):
        raise HttpError(429, "this invite was just emailed — wait a minute before sending another")
    try:
        inv = invites.reissue_invite(invite=inv)
    except invites.InviteError as exc:
        raise HttpError(_INVITE_ERROR_STATUS[exc.code], f"invite {exc.code.replace('_', ' ')}")
    return _invite_out(inv, notifications.email_invite(invite=inv))


@router.get("/invites/{token}/preview", response=InvitePreviewOut, auth=None,
            summary="Preview an invite before login (pre-auth, minimal disclosure)")
@gate("signed-link")  # auth=None; the invite token is the capability
def preview_invite(request: HttpRequest, token: str) -> InvitePreviewOut:
    try:
        inv = WorkspaceInvite.objects.select_related("workspace").get(token=token)
    except WorkspaceInvite.DoesNotExist:
        raise HttpError(404, "invite not found")
    status = invites.invite_status(inv)
    hint = invites.mask_email(inv.email)
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
@gate("authenticated", "signed-link", setting=True)
def accept_invite(request: HttpRequest, token: str) -> WorkspaceOut:
    try:
        ws, role = invites.accept_invite(token=token, user=request.user)
    except invites.InviteError as exc:
        status = _INVITE_ERROR_STATUS[exc.code]
        raise HttpError(status, exc.code)
    return _out(ws, role)


@router.get("/{slug}/analytics", response=WorkspaceAnalyticsOut,
            summary="Turns, chats, messages, tokens and people over time, by agent, repository and person")
@gate("logs.read")  # same tree read
def workspace_analytics(request: HttpRequest, slug: str, days: int = 30,
                        tree: bool = True) -> WorkspaceAnalyticsOut:
    """Daily series plus per-agent, per-repository and per-person tables for the last
    `days` (1-365) UTC days, over this workspace and, with `tree`, every one below it
    the caller may read. A repository is work no agent ran: a repo turn, or a
    person's own session in a repo. Counts come from the analytics fact log, which
    retention does not purge; tokens cover the turns and answers whose usage was
    seen (`totals.turns_with_usage`). No cost: no run reports one that describes a
    subscription. Admin and above."""
    from apps.events import analytics

    root, visible = _tree_reader(request, slug)
    slugs = [ws.slug for ws, _ in services.subtree(root, visible)] if tree else [root.slug]
    return WorkspaceAnalyticsOut(**analytics.build(slugs, days=days))


@router.get("/{slug}/network", response=WorkspaceNetworkOut,
            summary="Who talks to whom: people, contacts and agents linked by the threads they wrote in")
@gate("logs.read")  # same tree read; thread titles only where the session ACL lets the viewer open them
def workspace_network(request: HttpRequest, slug: str, days: int = 30,
                      tree: bool = True) -> WorkspaceNetworkOut:
    """People, contacts and agents over the last `days` (1-365) UTC days, each linked
    to the threads they wrote in — a chat (a bound Slack thread is one too) or an
    email thread — and each thread to the agent that answered it, across this
    workspace and, with `tree`, every one below it the caller may read. A thread's
    title is given only when the caller may open that chat. Read from the analytics
    fact log, which retention does not purge. Admin and above."""
    from apps.events import analytics

    root, visible = _tree_reader(request, slug)
    slugs = [ws.slug for ws, _ in services.subtree(root, visible)] if tree else [root.slug]
    return WorkspaceNetworkOut(**analytics.network(slugs, viewer=request.user, days=days))

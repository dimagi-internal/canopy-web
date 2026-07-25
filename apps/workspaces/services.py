"""Tenancy service helpers — the non-breaking glue for scoping agents to a
workspace: a default workspace, domain auto-join, and membership lookups.

Runtime counterparts to the one-time backfill data migration. Auto-join is what
keeps NEW domain users (and the board UI) seeing the default workspace's agents
after scoping turns on.
"""
from __future__ import annotations

import datetime as dt

from django.conf import settings
from django.contrib.auth import get_user_model
from django.utils import timezone

from .models import Workspace, WorkspaceInvite, WorkspaceMembership

DEFAULT_WORKSPACE_SLUG = "dimagi"
DEFAULT_WORKSPACE_NAME = "Dimagi"

INVITE_TTL_DAYS = 14


def allowed_domains() -> list[str]:
    raw = getattr(settings, "AUTH_ALLOWED_EMAIL_DOMAIN", "") or ""
    return [d.strip().lower() for d in raw.split(",") if d.strip()]


def _email_domain(email: str) -> str:
    email = (email or "").strip().lower()
    return email.rsplit("@", 1)[-1] if "@" in email else ""


def ensure_default_workspace() -> Workspace | None:
    """Return the default workspace, creating it (owned by the first superuser,
    else the first user) on first call. Returns None if there are no users yet
    (a fresh DB) — there is nothing to scope then."""
    ws = Workspace.objects.filter(slug=DEFAULT_WORKSPACE_SLUG).first()
    if ws is not None:
        return ws
    User = get_user_model()
    owner = (
        User.objects.filter(is_superuser=True).order_by("id").first()
        or User.objects.order_by("id").first()
    )
    if owner is None:
        return None
    ws = Workspace.objects.create(
        slug=DEFAULT_WORKSPACE_SLUG,
        display_name=DEFAULT_WORKSPACE_NAME,
        created_by=owner,
        auto_join_domains=allowed_domains(),
    )
    ensure_member(ws, owner, WorkspaceMembership.OWNER)
    return ws


def ensure_member(ws: Workspace, user, role: str = WorkspaceMembership.EDITOR) -> WorkspaceMembership:
    m, _ = WorkspaceMembership.objects.get_or_create(
        workspace=ws, user=user, defaults={"role": role}
    )
    return m


def auto_join_workspaces(user) -> None:
    """Add `user` (as editor) to every workspace whose auto_join_domains include
    their email domain. Cheap + idempotent; safe to call per request."""
    domain = _email_domain(getattr(user, "email", ""))
    if not domain:
        return
    for ws in Workspace.objects.exclude(auto_join_domains=[]):
        if domain in [d.lower() for d in (ws.auto_join_domains or [])]:
            ensure_member(ws, user, WorkspaceMembership.EDITOR)


def user_workspace_slugs(user) -> set[str]:
    return set(
        WorkspaceMembership.objects.filter(user=user).values_list("workspace_id", flat=True)
    )


def is_member(user, slug: str) -> bool:
    return WorkspaceMembership.objects.filter(user=user, workspace_id=slug).exists()


def request_workspace_slugs(request) -> set[str]:
    """The workspace slugs THIS request may act within — the single place a flat
    (`/api/…`) handler gets its tenant scope, so scoping can't drift per endpoint.

    A pinned `/api/w/{ws}/…` request was already membership-checked by
    WorkspaceResolveMiddleware, so trust that one slug. Otherwise it's the UNION of
    the authenticated user's memberships (so a human in dimagi+connect+family sees
    all three), and the empty set for anonymous callers.

    Filter list querysets by `workspace_id__in=request_workspace_slugs(request)`,
    and gate a by-id read/mutation with `obj.workspace_id in` it. This is the hard
    tenant boundary: data outside the caller's workspaces is unreachable."""
    pinned = getattr(request, "workspace_slug", None)
    if pinned:
        return {pinned}
    user = getattr(request, "user", None)
    if user is None or not user.is_authenticated:
        return set()
    # Domain teammates join their org's workspaces on first touch of any scoped
    # endpoint — the same "join on first touch" this repo already does per-handler,
    # centralized here so by-id gates and lists agree on who's a member.
    auto_join_workspaces(user)
    return user_workspace_slugs(user)


def workspace_slugs_for_user_id(user_id) -> set[str]:
    """The workspace slugs a user (by pk) may act within — the MCP-side counterpart
    of `request_workspace_slugs`, for tools that carry a token subject rather than a
    request. Empty set for an unresolvable user, so a scoped query fails CLOSED
    (sees/clears nothing) instead of falling back to global."""
    if user_id is None:
        return set()
    from django.contrib.auth import get_user_model

    user = get_user_model().objects.filter(pk=user_id).first()
    if user is None:
        return set()
    auto_join_workspaces(user)
    return user_workspace_slugs(user)


def workspace_member_ids(ws) -> list[int]:
    """User ids of every member of a workspace. Used to fan a supervisor update
    out to each member's live socket."""
    return list(ws.memberships.values_list("user_id", flat=True))


def user_default_workspace(user) -> Workspace | None:
    """The user's workspace when unambiguous — their sole membership, else None
    (0 or 2+ memberships). Used to resolve a default for headless PAT callers."""
    rows = list(
        WorkspaceMembership.objects.filter(user=user).select_related("workspace")[:2]
    )
    return rows[0].workspace if len(rows) == 1 else None


def current_workspace(user, explicit: str | None = None) -> Workspace:
    """Resolve the workspace a caller is acting in.

    explicit slug (caller must be a member) -> that workspace;
    else the caller's sole membership; else ValueError (none / ambiguous).
    Single resolution point for PAT callers, MCP tools, and the flat compat shim.
    """
    if explicit:
        ws = Workspace.objects.filter(slug=explicit).first()
        if ws is None or not is_member(user, explicit):
            raise ValueError(f"workspace '{explicit}' not found or not a member")
        return ws
    ws = user_default_workspace(user)
    if ws is None:
        raise ValueError("no unambiguous workspace for user; specify one")
    return ws


# ---- invites ----


class InviteError(Exception):
    """Raised by `accept_invite` for any reason acceptance can't proceed.

    `.code` is a closed set an HTTP layer (or the OAuth login gate in Task 2)
    maps to a status/decision: not_found, expired, revoked, already_accepted,
    email_mismatch.
    """

    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


def create_invite(
    *, workspace: Workspace, email: str, role: str, invited_by
) -> WorkspaceInvite:
    """Create (or reuse) an invite for `email` to join `workspace` at `role`.

    A live pending invite for the same (workspace, email) is reused as-is
    rather than duplicated — the existing row's role/token/expiry stand; the
    caller's `role` is ignored on reuse. Email is normalized to lowercase.
    """
    email = (email or "").strip().lower()
    existing = (
        WorkspaceInvite.objects.filter(
            workspace=workspace, email=email, accepted_at__isnull=True, revoked_at__isnull=True
        )
        .order_by("-created_at")
        .first()
    )
    if existing is not None and existing.is_pending():
        return existing
    return WorkspaceInvite.objects.create(
        workspace=workspace,
        email=email,
        role=role,
        invited_by=invited_by,
        expires_at=timezone.now() + dt.timedelta(days=INVITE_TTL_DAYS),
    )


def accept_invite(*, token: str, user) -> tuple[Workspace, str]:
    """Accept an invite by token on behalf of `user`.

    Returns (workspace, role) — role is the user's resulting membership role,
    which is their EXISTING role if they were already a member at a different
    role (get_or_create semantics: acceptance never changes an existing role).
    Raises InviteError on any invalid state; never mutates on failure.
    """
    try:
        inv = WorkspaceInvite.objects.select_related("workspace").get(token=token)
    except WorkspaceInvite.DoesNotExist:
        raise InviteError("not_found")
    if inv.accepted_at is not None:
        raise InviteError("already_accepted")
    if inv.revoked_at is not None:
        raise InviteError("revoked")
    if inv.expires_at <= timezone.now():
        raise InviteError("expired")
    if inv.email and (getattr(user, "email", "") or "").lower() != inv.email.lower():
        raise InviteError("email_mismatch")
    m, _ = WorkspaceMembership.objects.get_or_create(
        workspace=inv.workspace, user=user,
        defaults={"role": inv.role, "invited_by": inv.invited_by},
    )
    inv.accepted_at = timezone.now()
    inv.save(update_fields=["accepted_at"])
    return inv.workspace, m.role


def revoke_invite(*, invite: WorkspaceInvite) -> None:
    """Revoke a pending invite. Idempotent: a no-op on an already-revoked or
    already-accepted invite (never reopens an accepted one). Refreshes from
    the DB first so a caller holding a stale in-memory copy (e.g. fetched
    before a concurrent accept) still gets the correct, current guard."""
    invite.refresh_from_db(fields=["accepted_at", "revoked_at"])
    if invite.accepted_at is None and invite.revoked_at is None:
        invite.revoked_at = timezone.now()
        invite.save(update_fields=["revoked_at"])


def pending_invite_for_email(email: str) -> WorkspaceInvite | None:
    """The most recent LIVE (pending) invite addressed to `email`, or None.

    Case-insensitive; ignores expired/revoked/accepted rows. Used by the OAuth
    login adapter (Task 2) to auto-join a fresh signup to the workspace that
    invited them.
    """
    email = (email or "").strip().lower()
    if not email:
        return None
    candidate = (
        WorkspaceInvite.objects.filter(
            email=email, accepted_at__isnull=True, revoked_at__isnull=True
        )
        .order_by("-created_at")
        .first()
    )
    if candidate is not None and candidate.is_pending():
        return candidate
    return None

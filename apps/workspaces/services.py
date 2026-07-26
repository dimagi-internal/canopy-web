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
from django.db import IntegrityError, transaction
from django.utils import timezone

from .models import Workspace, WorkspaceInvite, WorkspaceMembership, generate_invite_token

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


# ---- members ----


class MemberError(Exception):
    """Raised by `set_member_role` / `remove_member` for any reason a member
    mutation can't proceed.

    `.code` is a closed set (`not_found`, `last_owner`, `invalid_role`) an
    HTTP layer maps to a status — same shape as `InviteError` below.
    """

    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


def is_last_owner(m: WorkspaceMembership) -> bool:
    """True iff `m` is an owner membership and the only owner left in its
    workspace. Shared by `remove_member` and `set_member_role` below so the
    "can't strand a workspace without an owner" guard lives in exactly one
    place. Must only be called from inside `_guarded_owner_mutation`'s
    transaction — see its docstring for why an un-locked call here is a
    TOCTOU hazard."""
    return m.role == WorkspaceMembership.OWNER and (
        WorkspaceMembership.objects.filter(
            workspace_id=m.workspace_id, role=WorkspaceMembership.OWNER
        ).count()
        == 1
    )


def _guarded_owner_mutation(*, workspace: Workspace, user_id, mutate):
    """The single choke point for any mutation that can remove a workspace's
    last owner (a role change OR a removal) — both `set_member_role` and
    `remove_member` go through this so the "never zero owners" guard has
    exactly one transaction + lock boundary instead of two independent
    check-then-act windows.

    TOCTOU this closes: workspace with owners A and B. A demotes/removes B
    while B (in a truly concurrent request) demotes/removes A. Un-guarded,
    both read count(owner)==2, both pass `is_last_owner`, both commit — zero
    owners, workspace permanently unmanageable via the API. Locking every
    currently-OWNER row of the workspace (`select_for_update`) BEFORE
    fetching the target row means the second transaction blocks on the
    first's commit; Postgres then re-evaluates the WHERE clause against the
    post-commit row versions (EvalPlanQual), so by the time it proceeds the
    now-demoted-from-owner row no longer matches `role=OWNER` and the count
    correctly reads 1 — the second mutation is correctly rejected instead of
    both racing through. (sqlite — this repo's test DB — has no row locking;
    `has_select_for_update` is False so Django silently drops the FOR UPDATE
    clause. See test_member_roles.py's TOCTOU tests for what can and can't be
    proven against that backend.)

    `mutate(m)` does the actual role-change/delete once the target row (and,
    if it's currently an owner, the lock) is in hand; it may raise
    `MemberError('last_owner')` itself via `is_last_owner`.
    """
    with transaction.atomic():
        # Lock every currently-OWNER row of this workspace first, before even
        # fetching the target — this is what makes a concurrent mutation on a
        # DIFFERENT owner of the same workspace serialize against this one.
        list(
            WorkspaceMembership.objects.select_for_update().filter(
                workspace_id=workspace.pk, role=WorkspaceMembership.OWNER
            )
        )
        try:
            m = WorkspaceMembership.objects.select_related("user").get(
                workspace=workspace, user_id=user_id
            )
        except WorkspaceMembership.DoesNotExist:
            raise MemberError("not_found")
        return mutate(m)


def set_member_role(*, workspace: Workspace, user_id, role: str) -> WorkspaceMembership:
    """Change a member's role. Idempotent: setting the role a member already
    holds succeeds as a no-op (and, deliberately, never trips the last-owner
    guard — see `test_set_member_role_is_idempotent`). Raises
    `MemberError('invalid_role')` if `role` isn't one `ROLE_RANK` knows (a
    caller that bypassed the HTTP schema's `Literal`, e.g. a shell/MCP call —
    `.save()` doesn't enforce Django `choices`, and an unranked role would
    later KeyError inside `accept_invite`), `MemberError('not_found')` if
    `user_id` isn't a member of `workspace`, or `MemberError('last_owner')`
    if this would demote the workspace's only remaining owner. See
    `_guarded_owner_mutation` for the transaction/lock this runs inside.
    """
    if role not in WorkspaceMembership.ROLE_RANK:
        raise MemberError("invalid_role")

    def _mutate(m: WorkspaceMembership) -> WorkspaceMembership:
        if role != m.role:
            if is_last_owner(m):
                raise MemberError("last_owner")
            m.role = role
            m.save(update_fields=["role"])
        return m

    return _guarded_owner_mutation(workspace=workspace, user_id=user_id, mutate=_mutate)


def remove_member(*, workspace: Workspace, user_id) -> None:
    """Remove a member. Raises `MemberError('not_found')` if `user_id` isn't
    a member of `workspace`, or `MemberError('last_owner')` if this would
    remove the workspace's only remaining owner. See `_guarded_owner_mutation`
    for the transaction/lock this runs inside — moved here (out of the API
    view) so it shares that boundary with `set_member_role` instead of each
    route running its own independent, unlocked check-then-act.
    """

    def _mutate(m: WorkspaceMembership) -> None:
        if is_last_owner(m):
            raise MemberError("last_owner")
        m.delete()

    _guarded_owner_mutation(workspace=workspace, user_id=user_id, mutate=_mutate)


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

    A "live" row for the same (workspace, email) — meaning never accepted,
    never revoked — is reused REGARDLESS of expiry: this is the same row the
    partial unique constraint treats as live (its condition doesn't consider
    `expires_at`), so an ordinary re-invite after a lapsed 14-day TTL must
    re-arm that same row rather than try to create a sibling and collide with
    it. Reuse re-arms the row in place (fresh token, fresh expiry, the
    newly-requested role) whenever anything about the request actually
    changed: the TTL lapsed, OR the requested role differs from what the row
    already holds. That second case is not cosmetic — it closes a real
    integrity bug: without it, re-inviting someone at a DIFFERENT role while
    their first invite is still pending returned the *same* row still
    carrying the FIRST role (e.g. an owner invites b@ as owner, immediately
    corrects it to editor — the still-live row kept reading "owner"). The
    owner then hands out a link that promotes b@ to owner regardless of what
    the UI most recently showed, and since `accept_invite` is upgrade-only,
    an already-lower-privileged b@ accepting it is a real, silent promotion —
    not the harmless no-op it used to be under the old get_or_create accept
    semantic. A request that changes nothing (same role, still pending) is a
    true no-op: the token is NOT rotated, so an already-shared link keeps
    working. Email is normalized to lowercase. Belt-and-braces: if a
    genuinely concurrent call still races past the `existing` check, the
    `.create()` IntegrityError is caught and the winning row is re-fetched
    and returned instead of raising.
    """
    email = (email or "").strip().lower()
    existing = (
        WorkspaceInvite.objects.filter(
            workspace=workspace, email=email, accepted_at__isnull=True, revoked_at__isnull=True
        )
        .order_by("-created_at")
        .first()
    )
    if existing is not None:
        stale = not existing.is_pending()  # never actioned, but its TTL lapsed
        role_changed = existing.role != role
        if stale or role_changed:
            existing.token = generate_invite_token()
            existing.expires_at = timezone.now() + dt.timedelta(days=INVITE_TTL_DAYS)
            existing.role = role
            existing.save(update_fields=["token", "expires_at", "role"])
        return existing
    try:
        return WorkspaceInvite.objects.create(
            workspace=workspace,
            email=email,
            role=role,
            invited_by=invited_by,
            expires_at=timezone.now() + dt.timedelta(days=INVITE_TTL_DAYS),
        )
    except IntegrityError:
        # A concurrent caller won the race and inserted the live row first;
        # return it instead of surfacing an unhandled 500 to this caller.
        return WorkspaceInvite.objects.get(
            workspace=workspace, email=email, accepted_at__isnull=True, revoked_at__isnull=True
        )


def accept_invite(*, token: str, user) -> tuple[Workspace, str]:
    """Accept an invite by token on behalf of `user`.

    Returns (workspace, role) — role is the user's resulting membership role.
    UPGRADE-ONLY semantic: if the user is already a member, acceptance moves
    their role to the HIGHER of their existing role and the invite's role
    (`WorkspaceMembership.ROLE_RANK`), and never demotes — a stray invite at a
    lower role than someone already holds must not be able to strip access.
    Explicit demotion is the owner-only PATCH `/members/{user_id}/`
    (`set_member_role`), never a side effect of accepting an invite. The
    invite itself is always consumed (marked accepted) on success, whether or
    not the role actually changed. Raises InviteError on any invalid state;
    never mutates on failure.
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
    m, created = WorkspaceMembership.objects.get_or_create(
        workspace=inv.workspace, user=user,
        defaults={"role": inv.role, "invited_by": inv.invited_by},
    )
    if not created and WorkspaceMembership.ROLE_RANK[inv.role] > WorkspaceMembership.ROLE_RANK[m.role]:
        m.role = inv.role
        m.save(update_fields=["role"])
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


def invite_status(invite: WorkspaceInvite) -> str:
    """Classify an invite's current lifecycle state for the pre-auth preview
    endpoint. One of `pending` | `expired` | `revoked` | `accepted` — checked
    in the same priority order as `accept_invite`'s guards (accepted wins over
    revoked, which wins over a lapsed TTL), so the two never disagree."""
    if invite.accepted_at is not None:
        return "accepted"
    if invite.revoked_at is not None:
        return "revoked"
    if invite.expires_at <= timezone.now():
        return "expired"
    return "pending"


def mask_email(email: str) -> str:
    """Mask an email's local part for pre-auth disclosure: keep the domain (so
    the right person recognizes their own invite) but never reveal the full
    local part — not even a 1-2 char one. Keeps only the first character and
    replaces the rest with a fixed-width mask (never leaks the local part's
    actual length either)."""
    email = (email or "").strip()
    if "@" not in email:
        return "•••"
    local, domain = email.split("@", 1)
    if not local:
        return f"•••@{domain}"
    return f"{local[0]}•••@{domain}"


def pending_invite_for_email(email: str) -> WorkspaceInvite | None:
    """The most recent LIVE (pending) invite addressed to `email`, or None.

    Case-insensitive; ignores expired/revoked/accepted rows. Used by the OAuth
    login adapter to help decide whether an otherwise-non-allowlisted email
    may clear the login gate (see `email_admitted_outside_domain`). This
    function does NOT itself join anyone to anything — it does not auto-join
    a fresh signup, and never will: the ONLY path that creates a
    WorkspaceMembership from an invite is `accept_invite`, called explicitly
    by the user after they sign in. Do not "restore" auto-join behavior here.
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


def email_admitted_outside_domain(email: str) -> bool:
    """Whether `email` may clear the OAuth login gate despite failing the
    global domain allowlist — the right question is NOT just "is there a
    pending invite", it's "does this email have any legitimate workspace
    standing right now": either it already belongs to a user holding at
    least one `WorkspaceMembership` (e.g. a previously-accepted invitee), OR
    there is a currently-live invite for it.

    Treating "pending invite only" as the whole answer 403s an accepted
    invitee on their very next login (accepting clears `pending`), which
    just pushes operators toward leaving invites open indefinitely — a
    strictly worse security posture than checking membership too. The
    trade-off this accepts: removing a user's last `WorkspaceMembership`
    does not revoke an already-open browser session, only their NEXT login.

    This is a LOGIN-GATE check only — it never creates, and must never be
    made to create, a `WorkspaceMembership`; only `accept_invite` does that.
    """
    email = (email or "").strip().lower()
    if not email:
        return False
    if WorkspaceMembership.objects.filter(user__email__iexact=email).exists():
        return True
    return pending_invite_for_email(email) is not None


def can_create_workspace(user) -> bool:
    """Gate on workspace CREATION (not on membership/access itself): an
    invite-admitted user who is not yet a member of anything must not be
    able to bootstrap their own workspace and mint invites of their own —
    otherwise invite-admission is transitively delegable to an attacker
    (create a workspace, invite arbitrary addresses, each newly-invited
    address then clears the login gate too) — see the F1 security finding
    on the invite-aware login gate.

    Allowed iff EITHER the caller's own email domain is on the global
    allowlist (an ordinary Dimagi/partner account, not merely
    invite-admitted), OR the caller already holds at least one
    `WorkspaceMembership` (so an accepted invitee has the same ordinary
    standing as anyone else once they've actually joined something).
    """
    from apps.common.auth_domains import email_in_allowlist

    email = getattr(user, "email", "") or ""
    if email_in_allowlist(email):
        return True
    return WorkspaceMembership.objects.filter(user=user).exists()

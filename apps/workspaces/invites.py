"""Invites: an admin invites an address at a role; the person accepts it after
signing in. Accepting is the only way an invite creates a membership (`_grant`)."""
from __future__ import annotations

import datetime as dt

from django.db import IntegrityError, transaction
from django.db.models import Q
from django.utils import timezone

from .models import Workspace, WorkspaceInvite, generate_invite_token
from .services import _grant

INVITE_TTL_DAYS = 14

# Gmail delivers every dot spelling of an address to one account and Google
# signs that account in under the spelling it was CREATED with, so an invite to
# marshallleejackson@ never matched the marshall.lee.jackson@ that Google
# reported for the same person: sign-in refused him, and accept would have too.
_GMAIL_DOMAINS = ("gmail.com", "googlemail.com")


def mailbox_key(email: str) -> str:
    """`email` reduced to the mailbox it delivers to: lowercased, and for Gmail
    without the dots in its local part (and googlemail.com as gmail.com).

    Only dots, only Gmail — Google guarantees those are one account, which is
    what lets an invite to one spelling admit and be accepted by another
    without widening who can accept it. Plus-addressing and other providers'
    rules are deliberately not folded: a different string there may be a
    different person."""
    email = (email or "").strip().lower()
    local, sep, domain = email.partition("@")
    if sep and domain in _GMAIL_DOMAINS:
        return f"{local.replace('.', '')}@gmail.com"
    return email


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
    if inv.email and mailbox_key(getattr(user, "email", "")) != mailbox_key(inv.email):
        raise InviteError("email_mismatch")
    m = _grant(inv.workspace, user, inv.role, invited_by=inv.invited_by)
    inv.accepted_at = timezone.now()
    inv.save(update_fields=["accepted_at"])
    return inv.workspace, m.role


def reissue_invite(*, invite: WorkspaceInvite) -> WorkspaceInvite:
    """Send a fresh link for an invite nobody has acted on: a new token and a
    new 14-day expiry, same email and role. The OLD link stops working — that
    is the point when a link was lost or pasted somewhere it should not be,
    and it is what makes an expired invite usable again without retyping it.

    Unlike `create_invite` (which leaves a still-pending, unchanged invite's
    token alone so an already-shared link keeps working), this ALWAYS rotates:
    an owner pressing "new link" has asked for exactly that. Accepted and
    revoked invites are finished and raise InviteError rather than reopening —
    a revoked invite is re-offered by inviting again. Locks the row so a
    concurrent accept cannot be undone by a rotation racing it.
    """
    with transaction.atomic():
        inv = WorkspaceInvite.objects.select_for_update().get(pk=invite.pk)
        if inv.accepted_at is not None:
            raise InviteError("already_accepted")
        if inv.revoked_at is not None:
            raise InviteError("revoked")
        inv.token = generate_invite_token()
        inv.expires_at = timezone.now() + dt.timedelta(days=INVITE_TTL_DAYS)
        inv.save(update_fields=["token", "expires_at"])
    return inv


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

    Matched by `mailbox_key` (case, and Gmail's dots); ignores expired/revoked/accepted rows. Used by the OAuth
    login adapter to help decide whether an otherwise-non-allowlisted email
    may clear the login gate (see `email_admitted_outside_domain`). This
    function does NOT itself join anyone to anything — it does not auto-join
    a fresh signup, and never will: the ONLY path that creates a
    WorkspaceMembership from an invite is `accept_invite`, called explicitly
    by the user after they sign in. Do not "restore" auto-join behavior here.
    """
    candidate = next(iter(_unactioned_invites_for(email)), None)
    if candidate is not None and candidate.is_pending():
        return candidate
    return None


def pending_invites_for(email: str) -> list[WorkspaceInvite]:
    """Every live invite addressed to `email`'s mailbox, newest first — what a
    signed-in person may still accept. Like `pending_invite_for_email`, it joins
    nobody to anything: Home lists these so a person who signed in without
    opening their invite link can still find it."""
    return [inv for inv in _unactioned_invites_for(email) if inv.is_pending()]


def _unactioned_invites_for(email: str) -> list[WorkspaceInvite]:
    """Invites to `email`'s mailbox never accepted or revoked (expired ones
    included), newest first."""
    key = mailbox_key(email)
    if not key:
        return []
    live = WorkspaceInvite.objects.filter(
        accepted_at__isnull=True, revoked_at__isnull=True).select_related("workspace", "invited_by")
    if key.endswith("@gmail.com"):
        # Any dot spelling, at either Gmail domain: narrow in SQL, match exactly here.
        live = live.filter(Q(email__iendswith="@gmail.com") | Q(email__iendswith="@googlemail.com"))
    else:
        live = live.filter(email=key)
    return [inv for inv in live.order_by("-created_at") if mailbox_key(inv.email) == key]

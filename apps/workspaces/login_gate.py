"""The login gate and the creation gate: who may sign in from outside the domain
allowlist, and who may create a workspace. Neither ever creates a membership."""
from __future__ import annotations

from .invites import pending_invite_for_email
from .models import WorkspaceMembership
from .services import _email_domain


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
    if pending_invite_for_email(email) is not None:
        return True
    return sign_in_domain_workspace(email) is not None


def sign_in_domain_workspace(email: str):
    """The workspace that lets people at `email`'s domain sign in, or None: the domain is on
    its `access_request_domains` AND is one of its VERIFIED canopy-hosted mail domains.

    The second half is the proof of control. A domain on the request list alone was never a
    login grant (anyone can make a Google account at gmail.com), but a workspace whose DKIM
    records verified for the domain controls its DNS, so it decides who holds addresses
    there — e.g. a family's or a team's free Google Workspace logins (Jonathan, 2026-10-18).
    Signing in still grants nothing: the person can only ask to join, approved by an admin
    or at the workspace's auto-approve role. The caller must already have checked the email
    is PROVIDER-verified (`CustomSocialAccountAdapter`)."""
    domain = _email_domain(email)
    if not domain:
        return None
    for owner_of in _VERIFIED_DOMAIN_OWNERS:
        ws = owner_of(domain)
        if ws is not None and domain in [str(x).lower() for x in (ws.access_request_domains or [])]:
            return ws
    return None


#: Callables `domain -> Workspace | None` naming the workspace that has PROVED control of a
#: domain (a verified canopy-hosted mail domain). Registered by `apps.mail` from its
#: AppConfig.ready — framework code cannot import the product app (ARCHITECTURE.md).
_VERIFIED_DOMAIN_OWNERS: list = []


def register_verified_domain_owner(fn) -> None:
    if fn not in _VERIFIED_DOMAIN_OWNERS:
        _VERIFIED_DOMAIN_OWNERS.append(fn)


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

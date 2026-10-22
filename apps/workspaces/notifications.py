"""Email plumbing shared by invites, access requests and the new-root notice:
one send that never raises, the call-to-action button, and the invite email."""
from __future__ import annotations

import datetime as dt
import logging

from django.core.mail import EmailMultiAlternatives
from django.utils import timezone
from django.utils.html import escape

from .links import base_url, invite_link
from .models import Workspace, WorkspaceInvite, WorkspaceMembership

_access_log = logging.getLogger("apps.workspaces.access_requests")


def _who(user) -> str:
    if user is None:
        return "canopy"
    name = (user.get_full_name() or "").strip()
    return f"{name} <{user.email}>" if name and user.email else (name or user.email or f"user {user.pk}")


def _send(subject: str, text: str, html: str, to: str, reply_to: str | None = None) -> str:
    """One email: `sent` | `not_configured` | `failed`. Never raises."""
    message = EmailMultiAlternatives(subject=subject, body=text, to=[to],
                                     reply_to=[reply_to] if reply_to else None)
    message.attach_alternative(html, "text/html")
    try:
        return "sent" if message.send() else "not_configured"
    except Exception:  # noqa: BLE001
        _access_log.exception("access-request email failed: to=%s subject=%r", to, subject)
        return "failed"


def _button(link: str, label: str) -> str:
    return (f'<p><a href="{escape(link)}" style="display:inline-block;padding:10px 18px;'
            f"background:#c2410c;color:#ffffff;border-radius:6px;text-decoration:none;"
            f'font-weight:600">{escape(label)}</a></p>')


# The same invite is not emailed twice inside this window. Re-inviting an
# address whose invite is unchanged re-sends the SAME link, and an owner
# double-clicking should not put two copies in someone's inbox.
INVITE_EMAIL_MIN_INTERVAL = dt.timedelta(seconds=60)

_email_log = logging.getLogger("apps.workspaces.invite_email")


def recently_emailed(invite: WorkspaceInvite) -> bool:
    return invite.last_emailed_at is not None and (
        timezone.now() - invite.last_emailed_at < INVITE_EMAIL_MIN_INTERVAL
    )


def email_invite(*, invite: WorkspaceInvite) -> str:
    """Email the invite's link to the invited address. Returns what happened:

    - ``sent``: handed to the mail backend (``last_emailed_at`` stamped)
    - ``throttled``: this invite was emailed under a minute ago; nothing sent
    - ``not_configured``: delivery is off in this deployment
    - ``failed``: the backend raised; logged, never propagated

    Never raises: the invite exists whether or not the mail goes out, and the
    owner still has the link to copy. The caller shows the outcome so a failed
    send is not mistaken for a delivered one.
    """
    if recently_emailed(invite):
        return "throttled"
    workspace = invite.workspace
    inviter = invite.invited_by
    inviter_name = (inviter.get_full_name() or "").strip() or inviter.email
    link = invite_link(invite)
    expires = timezone.localtime(invite.expires_at).strftime("%-d %B %Y")
    subject = f"{inviter_name} invited you to {workspace.display_name} on Canopy"
    text = (
        f"{inviter_name} ({inviter.email}) invited you to join the "
        f"{workspace.display_name} workspace on Canopy as {invite.role}.\n\n"
        f"Accept the invite:\n{link}\n\n"
        f"Sign in with the Google account for {invite.email} — the invite only "
        f"works for that address. If you don't have a Canopy account yet, "
        f"signing in creates one.\n\n"
        f"This link expires on {expires}. If you weren't expecting it, ignore this email.\n"
    )
    html = (
        f"<p>{escape(inviter_name)} ({escape(inviter.email)}) invited you to join the "
        f"<strong>{escape(workspace.display_name)}</strong> workspace on Canopy as "
        f"{escape(invite.role)}.</p>"
        f'<p><a href="{escape(link)}" style="display:inline-block;padding:10px 18px;'
        f"background:#c2410c;color:#ffffff;border-radius:6px;text-decoration:none;"
        f'font-weight:600">Accept the invite</a></p>'
        f"<p>Sign in with the Google account for <strong>{escape(invite.email)}</strong> "
        f"&mdash; the invite only works for that address. If you don't have a Canopy "
        f"account yet, signing in creates one.</p>"
        f'<p style="color:#78716c;font-size:13px">This link expires on {escape(expires)}. '
        f"If you weren't expecting it, ignore this email.<br>{escape(link)}</p>"
    )
    message = EmailMultiAlternatives(
        subject=subject, body=text, to=[invite.email], reply_to=[inviter.email] if inviter.email else None,
    )
    message.attach_alternative(html, "text/html")
    try:
        sent = message.send()
    except Exception:
        _email_log.exception("invite email failed: invite=%s workspace=%s", invite.pk, workspace.slug)
        return "failed"
    if not sent:
        return "not_configured"
    invite.last_emailed_at = timezone.now()
    invite.save(update_fields=["last_emailed_at"])
    return "sent"


def notify_root_created(ws: Workspace, by_user) -> dict:
    """A new TOP-LEVEL workspace was created by someone an organisation counts as
    its own: email that organisation's root owners, so a team does not quietly
    end up outside its org's tree (Jonathan, 2026-10-16: two divisions' agents
    turned out to live in root workspaces nobody meant to create, invisible to
    the fleet conductor, who is an admin of the dimagi tree only).

    "Its own" = the creator's email domain is on a root workspace's
    `access_request_domains` (dimagi: dimagi.com, dimagi-associate.com). The
    creator is never told about their own act. The email names both one-step
    fixes: nest the workspace (`PUT /api/workspaces/<slug>/parent`, which needs
    an owner of both ends — so the creator invites the org owner as an owner
    first), or move its agents, with their history, into the org.

    BEST-EFFORT: never raises — a mail outage must not fail a create."""
    result: dict = {"emailed": [], "failed": [], "orgs": []}
    try:
        if ws.parent_id:
            return result
        email = (getattr(by_user, "email", "") or "").lower()
        _, _, domain = email.rpartition("@")
        if not domain:
            return result
        orgs = [r for r in Workspace.objects.filter(parent__isnull=True).exclude(slug=ws.slug)
                if domain in [str(d).lower().lstrip("@") for d in (r.access_request_domains or [])]]
        who = _who(by_user)
        for org in orgs:
            result["orgs"].append(org.slug)
            owners = [m.user for m in WorkspaceMembership.objects.filter(
                workspace=org, role=WorkspaceMembership.OWNER).select_related("user")]
            link = f"{base_url()}/w/{ws.slug}"
            subject = f"New top-level workspace '{ws.slug}' created outside {org.display_name} by {who}"
            text = (f"{who} created the workspace '{ws.display_name}' ({ws.slug}) at the top level "
                    f"of Canopy — not under {org.display_name}. Its agents and sessions are not "
                    f"visible to {org.display_name}'s owners or admins.\n\n"
                    f"To bring it in, either:\n"
                    f"  - nest the workspace: they invite you as an owner of {ws.slug}, then you run "
                    f"PUT /api/workspaces/{ws.slug}/parent {{\"parent\": \"{org.slug}\"}} "
                    f"(canopy-web MCP: set_workspace_parent), or\n"
                    f"  - move its agents (with their history) into {org.slug}.\n\n"
                    f"Workspace: {link}\n\nYou are getting this because you own "
                    f"{org.display_name} on Canopy and {who} has a {domain} address.\n")
            html = (f"<p>{escape(who)} created the workspace <strong>{escape(ws.display_name)}</strong> "
                    f"({escape(ws.slug)}) at the top level of Canopy — not under "
                    f"{escape(org.display_name)}. Its agents and sessions are not visible to "
                    f"{escape(org.display_name)}'s owners or admins.</p>"
                    f"<p>To bring it in, either:</p><ul>"
                    f"<li><strong>nest the workspace</strong>: they invite you as an owner of "
                    f"{escape(ws.slug)}, then you run <code>set_workspace_parent</code> "
                    f"(parent: {escape(org.slug)}), or</li>"
                    f"<li><strong>move its agents</strong> (with their history) into {escape(org.slug)}.</li>"
                    f"</ul>" + _button(link, "Open the workspace")
                    + f'<p style="color:#78716c;font-size:13px">You are getting this because you own '
                      f"{escape(org.display_name)} on Canopy and {escape(who)} has a "
                      f"{escape(domain)} address.</p>")
            for owner in owners:
                if not owner.email or owner.pk == getattr(by_user, "pk", None):
                    continue
                status = _send(subject, text, html, owner.email, reply_to=email or None)
                (result["emailed"] if status == "sent" else result["failed"]).append(owner.email)
    except Exception:  # noqa: BLE001
        _access_log.exception("notifying org owners of new root workspace %s failed", ws.slug)
        result["error"] = True
    return result

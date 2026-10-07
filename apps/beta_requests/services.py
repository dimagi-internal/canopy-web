"""Record a request for access, tell the person who answers them, and act on the answer.

The order is the point: the row is written FIRST and the mail is best-effort, so a
request survives an SES outage and can still be read on its page or in Django admin.

The email is a doorbell, not the place the decision is made: it links to the
request's page in canopy, where the reviewer picks a workspace and a role and
canopy sends the invite (`invite`), or declines it (`decline`).
"""
from __future__ import annotations

import logging
from datetime import timedelta
from html import escape

from django.conf import settings
from django.core.mail import EmailMultiAlternatives
from django.db import transaction
from django.utils import timezone

from .models import BetaRequest

log = logging.getLogger(__name__)

# A repeat from the same address inside this window is kept but not re-mailed:
# someone pressing the button twice is one request, not two emails.
REPEAT_WINDOW = timedelta(hours=24)
# Per-address ceiling on the anonymous form. Counted from the table, not a cache,
# so it holds across every task in the service.
IP_WINDOW = timedelta(hours=1)
IP_LIMIT = 5


class TooManyRequests(Exception):
    pass


class NotPending(Exception):
    """The request was already answered."""


def may_review(user) -> bool:
    """Who may read and answer beta requests: a superuser, or the person the
    requests are mailed to (`CANOPY_BETA_REQUESTS_TO`). A request names no
    workspace, so no workspace role can stand in — and its reason is often
    about the asker's organisation, which other tenants' admins have no business
    reading. Sending the invite additionally needs `members.manage` in the
    chosen workspace (the API checks it, as for any invite)."""
    if not getattr(user, "is_authenticated", False):
        return False
    if user.is_superuser:
        return True
    to = (getattr(settings, "CANOPY_BETA_REQUESTS_TO", "") or "").strip().lower()
    return bool(to) and (user.email or "").strip().lower() == to


def request_link(req: BetaRequest) -> str:
    """Absolute link to the request's page — the one the email carries."""
    return f"{settings.CANOPY_PUBLIC_BASE_URL.rstrip('/')}/beta-requests/{req.pk}"


def submit(*, email: str, reason: str, client_ip: str | None, user_agent: str) -> BetaRequest:
    now = timezone.now()
    if client_ip and BetaRequest.objects.filter(
        client_ip=client_ip, created_at__gte=now - IP_WINDOW
    ).count() >= IP_LIMIT:
        raise TooManyRequests()

    email = email.strip().lower()
    repeat = BetaRequest.objects.filter(email=email, created_at__gte=now - REPEAT_WINDOW).exists()
    req = BetaRequest.objects.create(
        email=email, reason=reason.strip(), client_ip=client_ip or None,
        user_agent=(user_agent or "")[:300],
    )
    req.notify_result = "skipped" if repeat else _notify(req)
    req.save(update_fields=["notify_result"])
    return req


def _notify(req: BetaRequest) -> str:
    """One email to the person who grants access: `sent` | `not_configured` | `failed`. Never raises."""
    to = getattr(settings, "CANOPY_BETA_REQUESTS_TO", "")
    if not to:
        return "not_configured"
    link = request_link(req)
    subject = f"Canopy access request: {req.email}"
    text = (
        f"{req.email} asked for access to Canopy.\n\n"
        f"Why they want access:\n{req.reason}\n\n"
        f"Approve or decline — pick the workspace and role, and Canopy emails them the invite:\n{link}\n\n"
        f"Replying to this email writes to {req.email}, if you want to ask them something first.\n"
    )
    reason_html = escape(req.reason).replace("\n", "<br>")
    html = (
        f"<p><b>{escape(req.email)}</b> asked for access to Canopy.</p>"
        f"<p><b>Why they want access:</b><br>{reason_html}</p>"
        f'<p><a href="{escape(link)}" style="display:inline-block;padding:10px 18px;'
        f"background:#c2410c;color:#ffffff;border-radius:6px;text-decoration:none;"
        f'font-weight:600">Approve or decline</a></p>'
        "<p style=\"color:#78716c;font-size:13px\">Pick the workspace and role there, and "
        f"Canopy emails them the invite. Replying to this email writes to {escape(req.email)}, "
        f"if you want to ask them something first.<br>{escape(link)}</p>"
    )
    message = EmailMultiAlternatives(subject=subject, body=text, to=[to], reply_to=[req.email])
    message.attach_alternative(html, "text/html")
    try:
        return "sent" if message.send() else "not_configured"
    except Exception:  # noqa: BLE001 — a mail failure must never lose the request
        log.exception("beta-request email failed: request=%s", req.pk)
        return "failed"


def invite(req: BetaRequest, *, workspace, role: str, by) -> tuple[BetaRequest, str]:
    """Approve: invite the requester to `workspace` at `role` and email them the
    link. Returns the request and the invite email's status (`sent` |
    `throttled` | `not_configured` | `failed` — the invite exists either way).
    The caller has checked `by` may invite at `role` there."""
    from apps.workspaces import services as ws_services

    with transaction.atomic():
        locked = BetaRequest.objects.select_for_update().get(pk=req.pk)
        if locked.status != BetaRequest.PENDING:
            raise NotPending()
        inv = ws_services.create_invite(workspace=workspace, email=locked.email, role=role, invited_by=by)
        locked.status = BetaRequest.INVITED
        locked.workspace = workspace
        locked.role = role
        locked.invite = inv
        locked.decided_by = by
        locked.decided_at = timezone.now()
        locked.save(update_fields=["status", "workspace", "role", "invite", "decided_by", "decided_at"])
    # After the commit: a mail failure must not undo the decision, and the
    # invite's link can still be copied from the workspace's Members page.
    return locked, ws_services.email_invite(invite=inv)


def decline(req: BetaRequest, *, by) -> BetaRequest:
    """Close the request without letting them in. Emails nobody: a reviewer who
    wants to tell them why replies to the notification email."""
    with transaction.atomic():
        locked = BetaRequest.objects.select_for_update().get(pk=req.pk)
        if locked.status != BetaRequest.PENDING:
            raise NotPending()
        locked.status = BetaRequest.DECLINED
        locked.decided_by = by
        locked.decided_at = timezone.now()
        locked.save(update_fields=["status", "decided_by", "decided_at"])
    return locked

"""Record a closed-beta request and tell the person who answers them.

The order is the point: the row is written FIRST and the mail is best-effort, so a
request survives an SES outage and can still be read in Django admin.
"""
from __future__ import annotations

import logging
from datetime import timedelta
from html import escape

from django.conf import settings
from django.core.mail import EmailMultiAlternatives
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
    """One email to the beta's reader: `sent` | `not_configured` | `failed`. Never raises."""
    to = getattr(settings, "CANOPY_BETA_REQUESTS_TO", "")
    if not to:
        return "not_configured"
    subject = f"Canopy beta request: {req.email}"
    text = (
        f"{req.email} asked to join the Canopy beta.\n\n"
        f"Why they want access:\n{req.reason}\n\n"
        "Reply to this email to answer them. To let them in, invite them to a "
        "workspace from its Settings → Members."
    )
    reason_html = escape(req.reason).replace("\n", "<br>")
    html = (
        f"<p><b>{escape(req.email)}</b> asked to join the Canopy beta.</p>"
        f"<p><b>Why they want access:</b><br>{reason_html}</p>"
        "<p style=\"color:#6b6b6b\">Reply to this email to answer them. To let them in, "
        "invite them to a workspace from its Settings → Members.</p>"
    )
    message = EmailMultiAlternatives(subject=subject, body=text, to=[to], reply_to=[req.email])
    message.attach_alternative(html, "text/html")
    try:
        return "sent" if message.send() else "not_configured"
    except Exception:  # noqa: BLE001 — a mail failure must never lose the request
        log.exception("beta-request email failed: request=%s", req.pk)
        return "failed"

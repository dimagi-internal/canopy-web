"""Keep one tenant from spending the labs account's sending reputation.

Thresholds sit under SES's own review lines (5 % bounce, 0.1 % complaint) so a
domain is stopped here before AWS pauses sending for connect-labs too.
"""
from __future__ import annotations

import datetime as dt
from email.utils import parseaddr

from django.db import IntegrityError, transaction
from django.utils import timezone
from django.utils.dateparse import parse_datetime

from .models import MailDomain, MailEvent, MailMessage

WINDOW = dt.timedelta(days=7)
MIN_SENDS = 50
MAX_BOUNCE = 0.04
MAX_COMPLAINT = 0.0008
BURST_WINDOW = dt.timedelta(hours=1)
BURST_HARD_BOUNCES = 10
_KINDS = ("Send", "Delivery", "Bounce", "Complaint", "Reject")
_STATUS = {"Delivery": MailMessage.DELIVERED, "Bounce": MailMessage.BOUNCED,
           "Complaint": MailMessage.COMPLAINED, "Reject": MailMessage.REJECTED}


def _domain_of(source) -> MailDomain | None:
    """Exact-match the sending domain; subdomain senders (bounce.x.com) are not tenant sends."""
    if not isinstance(source, str):
        return None
    _, addr = parseaddr(source)
    host = addr.rpartition("@")[2].lower().strip()
    return MailDomain.objects.filter(name=host).first() if host else None


def record(event: dict) -> None:
    kind = event.get("eventType") or event.get("notificationType") or ""
    mail = event.get("mail") or {}
    if kind not in _KINDS:
        return
    domain = _domain_of(mail.get("source"))
    if domain is None:
        return
    at = parse_datetime(mail.get("timestamp") or "") or timezone.now()
    msg_id = mail.get("messageId", "") or ""
    hard = kind == "Bounce" and (event.get("bounce") or {}).get("bounceType") == "Permanent"
    if msg_id:
        # SNS redelivers: one SES event of one kind counts once (unique on (ses_message_id, kind)).
        try:
            with transaction.atomic():
                MailEvent.objects.create(domain=domain, kind=kind, hard=hard, ses_message_id=msg_id, at=at)
        except IntegrityError:
            return
    else:
        MailEvent.objects.create(domain=domain, kind=kind, hard=hard, ses_message_id=msg_id, at=at)
    if kind in _STATUS and msg_id:
        MailMessage.objects.filter(ses_message_id=msg_id).update(delivery_status=_STATUS[kind])
    evaluate(domain)


def evaluate(domain: MailDomain, *, now=None) -> str | None:
    """Suspend the domain if it is over a limit; returns the reason (None if fine/already suspended)."""
    if domain.status == MailDomain.SUSPENDED:
        return None
    now = now or timezone.now()
    ev = MailEvent.objects.filter(domain=domain, at__gte=now - WINDOW)
    sends = ev.filter(kind="Send").count()
    reason = None
    burst = ev.filter(kind="Bounce", hard=True, at__gte=now - BURST_WINDOW).count()
    if burst > BURST_HARD_BOUNCES:
        reason = f"hard-bounce burst: {burst} in the last hour"
    elif sends >= MIN_SENDS:
        bounces = ev.filter(kind="Bounce").count() / sends
        complaints = ev.filter(kind="Complaint").count() / sends
        if bounces > MAX_BOUNCE:
            reason = f"bounce rate {bounces:.1%} over 7 days (limit {MAX_BOUNCE:.0%})"
        elif complaints > MAX_COMPLAINT:
            reason = f"complaint rate {complaints:.2%} over 7 days (limit {MAX_COMPLAINT:.2%})"
    if reason is None:
        return None
    # Filtered update: a concurrent admin unsuspend/suspend is never clobbered.
    changed = (MailDomain.objects.filter(pk=domain.pk).exclude(status=MailDomain.SUSPENDED)
               .update(status=MailDomain.SUSPENDED, suspended_reason=reason))
    if not changed:
        return None
    domain.status, domain.suspended_reason = MailDomain.SUSPENDED, reason
    return reason

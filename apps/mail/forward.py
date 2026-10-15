"""Forward a message to a workspace member, as the domain.

SES can only send as a verified identity, so the copy cannot keep the original
From; it says who it is from in the display name and carries Reply-To so a reply
reaches the original sender. The original DKIM/ARC signatures are removed — they
no longer verify over the rewritten headers.
"""
from __future__ import annotations

from datetime import timedelta
from email import policy
from email.parser import BytesParser
from email.utils import formataddr

from django.conf import settings
from django.db import IntegrityError, transaction
from django.utils import timezone

from apps.workspaces import services as wsvc

from . import domains, ses
from .models import MailForwardLog

_STRIP = ("DKIM-Signature", "ARC-Seal", "ARC-Message-Signature", "ARC-Authentication-Results",
          "Return-Path", "Disposition-Notification-To", "Return-Receipt-To", "Sender", "From", "Reply-To", "Message-ID")


def _rewrite(msg, route, parsed, recipient: str) -> bytes:
    for h in _STRIP:
        del msg[h]
    who = parsed.from_name or parsed.from_addr
    msg["From"] = formataddr((f"{who} via {route.domain.name}",
                              f"{settings.MAIL_FORWARDER_LOCAL_PART}@{route.domain.name}"))
    if parsed.from_addr:
        msg["Reply-To"] = formataddr((parsed.from_name, parsed.from_addr))
    msg["X-Original-From"] = parsed.from_addr
    msg["X-Original-To"] = recipient
    msg["Message-ID"] = f"<fwd-{parsed.message_id.strip('<>')}>"
    return msg.as_bytes()


def build(route, parsed, raw: bytes, recipient: str) -> bytes:
    try:
        return _rewrite(BytesParser(policy=policy.SMTP).parsebytes(raw), route, parsed, recipient)
    except Exception:  # a malformed original (bad charset/header) must not block forwarding
        return _rewrite(BytesParser(policy=policy.compat32).parsebytes(raw), route, parsed, recipient)


STALE_CLAIM = timedelta(minutes=10)  # a "sending" claim older than this was orphaned by a crash


def _claim(route, parsed, recipient, status, user, error="") -> MailForwardLog | None:
    """Insert the log row; the (domain, message_id, recipient) unique constraint is the lock."""
    try:
        with transaction.atomic():
            return MailForwardLog.objects.create(
                domain=route.domain, route=route, message_id=parsed.message_id, recipient=recipient,
                original_from=parsed.from_addr, subject=parsed.subject[:998],
                destination=user, status=status, error=error)
    except IntegrityError:
        return None


def _is_loop(email: str, domain: str) -> bool:
    """A destination on ANY canopy mail domain (this one or another workspace's) could route
    the copy straight back in; forward only to mail that leaves canopy."""
    host = email.rsplit("@", 1)[-1].lower().rstrip(".")
    domain = domain.lower()
    return host == domain or host.endswith("." + domain) or domains._domain_for(host) is not None


def forward(route, parsed, raw: bytes, recipient: str) -> str:
    user = route.forward_to
    drop = None
    if user is None or not wsvc.is_member(user, route.workspace_id):
        drop = ("dropped:not-a-member", "destination is no longer a member of this workspace")
    elif not (user.email or "").strip():
        drop = ("dropped:no-email", "destination has no email address")
    elif _is_loop(user.email.strip(), route.domain.name):
        drop = ("dropped:loop", "forwarding loop")
    if drop:
        if _claim(route, parsed, recipient, MailForwardLog.DROPPED, user, drop[1]) is None:
            return "duplicate"
        return drop[0]
    row = _claim(route, parsed, recipient, MailForwardLog.SENDING, user)
    if row is None:
        now = timezone.now()
        key = dict(domain=route.domain, message_id=parsed.message_id, recipient=recipient)
        if MailForwardLog.objects.filter(status=MailForwardLog.SENDING, created_at__lt=now - STALE_CLAIM,
                                         **key).update(created_at=now) != 1:
            return "duplicate"
        row = MailForwardLog.objects.get(**key)
    try:
        ses.send_raw(from_addr=f"{settings.MAIL_FORWARDER_LOCAL_PART}@{route.domain.name}",
                     to=[user.email], raw=build(route, parsed, raw, recipient))
    except Exception:
        row.delete()  # leave no claim so the SNS retry can forward
        raise
    row.status = MailForwardLog.FORWARDED
    row.save(update_fields=["status"])
    return "forwarded"

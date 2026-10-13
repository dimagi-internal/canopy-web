"""Forward a message to a workspace member, as the domain.

SES can only send as a verified identity, so the copy cannot keep the original
From; it says who it is from in the display name and carries Reply-To so a reply
reaches the original sender. The original DKIM/ARC signatures are removed — they
no longer verify over the rewritten headers.
"""
from __future__ import annotations

from email import policy
from email.parser import BytesParser
from email.utils import formataddr

from django.conf import settings
from django.db import IntegrityError, transaction

from apps.workspaces import services as wsvc

from . import ses
from .models import MailForwardLog

_STRIP = ("DKIM-Signature", "ARC-Seal", "ARC-Message-Signature", "ARC-Authentication-Results",
          "Return-Path", "Sender", "From", "Reply-To", "Message-ID")


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


def _log(route, parsed, recipient, status, error="", destination=None) -> MailForwardLog | None:
    try:
        with transaction.atomic():
            return MailForwardLog.objects.create(
                domain=route.domain, route=route, message_id=parsed.message_id, recipient=recipient,
                original_from=parsed.from_addr, subject=parsed.subject[:998],
                destination=destination, status=status, error=error)
    except IntegrityError:
        return None


def forward(route, parsed, raw: bytes, recipient: str) -> str:
    if MailForwardLog.objects.filter(domain=route.domain, message_id=parsed.message_id,
                                     recipient=recipient).exclude(status=MailForwardLog.FAILED).exists():
        return "duplicate"
    user = route.forward_to
    if user is None or not wsvc.is_member(user, route.domain.workspace_id):
        _log(route, parsed, recipient, MailForwardLog.DROPPED,
             "destination is no longer a member of this workspace", destination=user)
        return "dropped:not-a-member"
    MailForwardLog.objects.filter(domain=route.domain, message_id=parsed.message_id,
                                  recipient=recipient, status=MailForwardLog.FAILED).delete()
    ses.send_raw(from_addr=f"{settings.MAIL_FORWARDER_LOCAL_PART}@{route.domain.name}",
                 to=[user.email], raw=build(route, parsed, raw, recipient))
    _log(route, parsed, recipient, MailForwardLog.FORWARDED, destination=user)
    return "forwarded"

"""An agent's mailbox: mail in becomes the agent's email turn; mail out is sent as it."""
from __future__ import annotations

from django.conf import settings
from django.db import IntegrityError, transaction
from django.db.models import F
from django.utils import timezone

from apps.harness import services as harness
from apps.harness.models import Turn

from . import ses, threads
from .models import MailAttachment, MailMessage


def _auth_results(parsed) -> list[dict]:
    return [{"name": k, "value": v} for k, v in parsed.headers if k.lower() == "authentication-results"]


def deliver(route, parsed, raw_key: str) -> str:
    ws = route.domain.workspace
    if MailMessage.objects.filter(workspace=ws, mailbox=route.address, message_id=parsed.message_id).exists():
        return "duplicate"
    thread = threads.place(route, parsed)
    try:
        with transaction.atomic():
            msg = MailMessage.objects.create(
                thread=thread, workspace=ws, direction=MailMessage.IN, mailbox=route.address,
                message_id=parsed.message_id, in_reply_to=parsed.in_reply_to,
                references=" ".join(parsed.references), from_addr=parsed.from_addr,
                from_name=parsed.from_name, to=parsed.to, cc=parsed.cc,
                subject=parsed.subject[:998], text=parsed.text, html=parsed.html,
                raw_key=ses.keep(raw_key),
                auth_results="\n".join(h["value"] for h in _auth_results(parsed)))
    except IntegrityError:
        return "duplicate"
    for i, att in enumerate(parsed.attachments):
        key = f"attachments/{ws.slug}/{msg.pk}/{i}"
        ses.put_object(key, att.data, att.content_type)
        MailAttachment.objects.create(message=msg, filename=att.filename[:255],
                                      content_type=att.content_type[:255], size=len(att.data), s3_key=key)
    type(thread).objects.filter(pk=thread.pk).update(
        message_count=F("message_count") + 1, last_message_at=timezone.now(), unread=True)
    thread.refresh_from_db()
    if parsed.from_addr == route.address:
        return "stored:own-mail"
    if parsed.automated:
        return "stored:automated"
    agent = route.agent
    harness.enqueue_turn(
        agent=agent, origin=Turn.ORIGIN_EMAIL,
        idempotency_key=f"email-{agent.slug}-{thread.uuid}-{thread.message_count}",
        prompt=f"/{agent.slug}:turn --thread {thread.uuid}",
        origin_ref={"thread_id": str(thread.uuid), "backend": "ses", "from": parsed.from_addr,
                    "from_name": parsed.from_name, "subject": parsed.subject,
                    "message_id": parsed.message_id, "discovered_by": "ses",
                    "message_count": thread.message_count, "headers": _auth_results(parsed)},
        authserv_id=settings.MAIL_SES_AUTHSERV_ID,
    )
    return "agent"

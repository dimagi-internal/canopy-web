"""An agent's mailbox: mail in becomes the agent's email turn; mail out is sent as it."""
from __future__ import annotations

import datetime as dt
import re
from email.message import EmailMessage
from email.utils import formataddr, formatdate, make_msgid

from django.conf import settings
from django.db import IntegrityError, transaction
from django.db.models import F
from django.utils import timezone

from apps.harness import services as harness
from apps.harness.models import Turn

from . import ses, threads
from .models import MailAttachment, MailDomain, MailMessage, MailThread


def _auth_results(parsed) -> list[dict]:
    return [{"name": k, "value": v} for k, v in parsed.headers if k.lower() == "authentication-results"]


def _quiet(route, parsed) -> str | None:
    """Why this inbound message must not wake the agent, or None."""
    if parsed.from_addr == route.address.lower():
        return "stored:own-mail"
    if parsed.automated:
        return "stored:automated"
    return None


def _wake(route, parsed, msg, thread) -> None:
    agent = route.agent
    harness.enqueue_turn(
        agent=agent, origin=Turn.ORIGIN_EMAIL,
        idempotency_key=_turn_key(agent, thread, msg),
        prompt=f"/{agent.slug}:turn --thread {thread.uuid}",
        origin_ref={"thread_id": str(thread.uuid), "backend": "ses", "from": parsed.from_addr,
                    "from_name": parsed.from_name, "subject": parsed.subject,
                    "message_id": parsed.message_id, "discovered_by": "ses",
                    "message_count": thread.message_count, "headers": _auth_results(parsed)},
        authserv_id=settings.MAIL_SES_AUTHSERV_ID,
    )


def _turn_key(agent, thread, msg) -> str:
    return f"email-{agent.slug}-{thread.uuid}-{msg.pk}"


def deliver(route, parsed, raw_key: str) -> str:
    ws = route.domain.workspace
    existing = (MailMessage.objects.filter(workspace=ws, mailbox=route.address,
                                           message_id=parsed.message_id)
                .select_related("thread").first())
    if existing is None:
        quiet = _quiet(route, parsed)
        try:
            with transaction.atomic():
                thread = threads.place(route, parsed)
                msg = MailMessage.objects.create(
                    thread=thread, workspace=ws, direction=MailMessage.IN, mailbox=route.address,
                    message_id=parsed.message_id, in_reply_to=parsed.in_reply_to,
                    references=" ".join(parsed.references), from_addr=parsed.from_addr,
                    from_name=parsed.from_name, to=parsed.to, cc=parsed.cc,
                    subject=parsed.subject[:998], text=parsed.text, html=parsed.html,
                    raw_key=ses.keep(raw_key),
                    auth_results="\n".join(h["value"] for h in _auth_results(parsed)))
                for i, att in enumerate(parsed.attachments):
                    key = f"attachments/{ws.slug}/{msg.pk}/{i}"
                    ses.put_object(key, att.data, att.content_type)
                    MailAttachment.objects.create(message=msg, filename=att.filename[:255],
                                                  content_type=att.content_type[:255],
                                                  size=len(att.data), s3_key=key)
                upd = {"message_count": F("message_count") + 1, "last_message_at": timezone.now()}
                if quiet is None:
                    upd["unread"] = True
                elif thread.message_count == 0:
                    upd["unread"] = False  # a brand-new thread of only quiet mail
                MailThread.objects.filter(pk=thread.pk).update(**upd)
        except IntegrityError:
            return "duplicate"
        thread.refresh_from_db()
        if quiet:
            return quiet
        _wake(route, parsed, msg, thread)
        return "agent"

    # Redelivery: heal a crash between commit and enqueue.
    if existing.direction != MailMessage.IN or _quiet(route, parsed):
        return "duplicate"
    agent = route.agent
    key = _turn_key(agent, existing.thread, existing)
    if Turn.objects.filter(idempotency_key=key).exists():
        return "duplicate"
    _wake(route, parsed, existing, existing.thread)
    return "agent"


class SendRefused(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status


_ADDR_RE = re.compile(r"^[^\s@<>,;\"()\[\]\\]+@[^\s@<>,;\"()\[\]\\]+\.[^\s@<>,;\"()\[\]\\]+$")
_LINE_BREAKS = re.compile(r"[\r\n\u2028\u2029\x0b\x0c\x85]+")


def _line(value: str) -> str:
    """A header value is one line: any line break becomes a space."""
    return _LINE_BREAKS.sub(" ", value or "").strip()


def _addresses(values, what: str) -> list[str]:
    out = []
    for raw in values or ():
        a = (raw or "").strip().lower()
        if not a:
            continue
        if len(a) > 320 or not _ADDR_RE.match(a):
            raise SendRefused(422, f"{what} {_line(raw)[:80]!r} is not an email address")
        out.append(a)
    return out


def _sent_today(domain) -> int:
    since = timezone.now() - dt.timedelta(days=1)
    return MailMessage.objects.filter(workspace=domain.workspace_id, direction=MailMessage.OUT,
                                      mailbox__endswith="@" + domain.name, created_at__gte=since).count()


def send(route, *, to, cc=(), subject="", text="", html="", thread=None, attachments=()) -> MailMessage:
    """Send as the route's agent. The From is always the route address."""
    domain = route.domain
    if domain.status == MailDomain.SUSPENDED:
        raise SendRefused(409, f"sending from {domain.name} is suspended: {domain.suspended_reason}")
    if domain.status != MailDomain.VERIFIED:
        raise SendRefused(409, f"{domain.name} is not verified yet")
    to, cc = _addresses(to, "recipient"), _addresses(cc, "cc")
    if not to:
        raise SendRefused(422, "no recipients")
    own = route.address.lower()
    if own in to or own in cc:
        raise SendRefused(422, "an agent cannot send mail to its own address")
    if _sent_today(domain) >= domain.daily_send_cap:
        raise SendRefused(429, f"{domain.name} reached its daily send cap ({domain.daily_send_cap})")
    subject = _line(subject)
    msg = EmailMessage()
    msg["From"] = formataddr((_line(route.agent.name) if route.agent_id else "", route.address))
    msg["To"] = ", ".join(to)
    if cc:
        msg["Cc"] = ", ".join(cc)
    msg["Date"] = formatdate(localtime=False)
    msg["Message-ID"] = make_msgid(domain=domain.name)
    if thread is not None:
        prior = list(thread.messages.order_by("created_at", "pk").values_list("message_id", flat=True))
        if prior:
            msg["In-Reply-To"] = _line(prior[-1])
            msg["References"] = " ".join(_line(p) for p in prior[-20:])
        base = _line(thread.subject)
        subject = subject or (base if base.lower().startswith("re:") else f"Re: {base}")
    msg["Subject"] = subject
    msg.set_content(text or "")
    if html:
        msg.add_alternative(html, subtype="html")
    for att in attachments:
        main, _, sub = (att.content_type or "application/octet-stream").partition("/")
        msg.add_attachment(att.data, maintype=main, subtype=sub or "octet-stream",
                           filename=_line(att.filename))
    ses_id = ses.send_raw(from_addr=route.address, to=to + cc, raw=msg.as_bytes())
    with transaction.atomic():
        if thread is None:
            thread = MailThread.objects.create(workspace=domain.workspace, route=route, subject=subject[:998])
        out = MailMessage.objects.create(
            thread=thread, workspace=domain.workspace, direction=MailMessage.OUT, mailbox=route.address,
            message_id=msg["Message-ID"], in_reply_to=msg.get("In-Reply-To", ""),
            references=msg.get("References", ""), from_addr=route.address, to=to, cc=cc,
            subject=subject[:998], text=text, html=html, ses_message_id=ses_id,
            delivery_status=MailMessage.SENT)
        MailThread.objects.filter(pk=thread.pk).update(
            message_count=F("message_count") + 1, last_message_at=timezone.now(), unread=False)
    return out

"""An agent's mailbox: mail in becomes the agent's email turn; mail out is sent as it."""
from __future__ import annotations

import datetime as dt
import logging
import re
from email.message import EmailMessage
from email.utils import formataddr, formatdate, make_msgid

from botocore.exceptions import BotoCoreError, ClientError
from django.conf import settings
from django.db import IntegrityError, transaction
from django.db.models import F
from django.utils import timezone

from apps.canopy_sessions.models import Session
from apps.harness import invocations, session_sync
from apps.harness import services as harness
from apps.harness.channels import email as email_channel
from apps.harness.models import Turn

from . import domains, ses, storage, threads
from .models import MailAttachment, MailDomain, MailMessage, MailThread

logger = logging.getLogger(__name__)


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
        prompt=invocations.command(agent.slug, "turn", f"--thread {thread.uuid}"),
        origin_ref={"thread_id": str(thread.uuid), "backend": "ses", "mailbox": route.address,
                    "from": parsed.from_addr,
                    "from_name": parsed.from_name, "subject": parsed.subject,
                    "message_id": parsed.message_id, "discovered_by": "ses",
                    # Who else is on the thread: the email door admits the members
                    # among them to its session (`email_channel.admit_thread_people`).
                    "to": list(parsed.to or []), "cc": list(parsed.cc or []),
                    "message_count": thread.message_count, "headers": _auth_results(parsed)},
        authserv_id=settings.MAIL_SES_AUTHSERV_ID,
        initiator=None,  # email derives its initiator from the sender inside enqueue_turn
    )


def _turn_key(agent, thread, msg) -> str:
    return f"email-{agent.slug}-{thread.uuid}-{msg.pk}"


def _enforce_cap(msg) -> None:
    """Keep the mailbox under its cap. Never fails the delivery or send that called it:
    the message is stored either way, and the next one tries again."""
    try:
        storage.enforce_cap(msg.workspace_id, msg.mailbox, keep=msg.pk)
    except Exception:  # noqa: BLE001
        logger.exception("mail cap %s: eviction failed", msg.mailbox)


def _store(route, parsed, raw_key: str, *, verdicts, raw_size: int, quiet: str | None, spam: bool = False):
    """Store one inbound copy under `route`'s mailbox. Returns (msg, thread), or None when
    this mailbox already holds the message. Mail SES called spam keeps its words only (no
    attachment or raw copy) and always starts its own thread: its In-Reply-To is the sender's
    to write, and must not put it inside a real conversation."""
    ws = route.workspace
    keep = ([False] * len(parsed.attachments) if spam
            else storage.kept_attachments([len(a.data) for a in parsed.attachments]))
    # The raw MIME holds every attachment, so a message that dropped one keeps no raw copy.
    whole = all(keep) and not spam
    try:
        with transaction.atomic():
            thread = (threads.start(route, parsed, spam=True) if spam else threads.place(route, parsed))
            msg = MailMessage.objects.create(
                thread=thread, workspace=ws, direction=MailMessage.IN, mailbox=route.address,
                message_id=parsed.message_id, in_reply_to=parsed.in_reply_to,
                references=" ".join(parsed.references), from_addr=parsed.from_addr,
                from_name=parsed.from_name, to=parsed.to, cc=parsed.cc,
                reply_to=parsed.reply_to, subject=parsed.subject[:998], text=parsed.text, html=parsed.html,
                raw_key=ses.keep(raw_key) if whole else "", verdicts=dict(verdicts or {}),
                size_bytes=storage.message_size(
                    text=parsed.text, html=parsed.html, raw_size=raw_size if whole else 0,
                    attachment_sizes=[len(a.data) for a, k in zip(parsed.attachments, keep) if k]),
                auth_results="\n".join(h["value"] for h in _auth_results(parsed)))
            for i, (att, kept) in enumerate(zip(parsed.attachments, keep)):
                key = f"attachments/{ws.slug}/{msg.pk}/{i}" if kept else ""
                if kept:
                    ses.put_object(key, att.data, att.content_type)
                MailAttachment.objects.create(message=msg, filename=att.filename[:255],
                                              content_type=att.content_type[:255],
                                              size=len(att.data), s3_key=key, kept=kept)
            upd = {"message_count": F("message_count") + 1, "last_message_at": timezone.now()}
            if quiet is None:
                # New mail on a conversation brings it back, as in any mail client. Someone's
                # "this is spam" stands, though: it does not get to climb back out by replying.
                upd.update(unread=True, archived=False, trashed_at=None)
            elif thread.message_count == 0:
                upd["unread"] = False  # a brand-new thread of only quiet mail
            MailThread.objects.filter(pk=thread.pk).update(**upd)
    except IntegrityError:
        return None
    _enforce_cap(msg)
    thread.refresh_from_db()
    return msg, thread


def keep_forwarded(route, parsed, raw_key: str, *, verdicts: dict | None = None, raw_size: int = 0) -> str:
    """Store a copy of mail a forward route passes on, as a thread only the destination can read."""
    if MailMessage.objects.filter(workspace=route.workspace, mailbox=route.address,
                                  message_id=parsed.message_id).exists():
        return "duplicate"
    stored = _store(route, parsed, raw_key, verdicts=verdicts, raw_size=raw_size, quiet=None)
    return "duplicate" if stored is None else "kept"


def keep_spam(route, parsed, *, verdicts: dict | None = None) -> str:
    """Put mail SES marked as spam in the mailbox's Spam folder: seen by nobody until they look,
    forwarded to no one, and it never wakes an agent."""
    stored = _store(route, parsed, "", verdicts=verdicts, raw_size=0, quiet="stored:spam", spam=True)
    return "duplicate" if stored is None else "stored:spam"


def deliver(route, parsed, raw_key: str, *, verdicts: dict | None = None, raw_size: int = 0) -> str:
    """Store one inbound copy for `route` and wake its agent. `raw_size` is the raw MIME
    object's length, counted against the mailbox's cap with the stored parts."""
    ws = route.workspace
    existing = (MailMessage.objects.filter(workspace=ws, mailbox=route.address,
                                           message_id=parsed.message_id)
                .select_related("thread").first())
    if existing is None:
        quiet = _quiet(route, parsed)
        stored = _store(route, parsed, raw_key, verdicts=verdicts, raw_size=raw_size, quiet=quiet)
        if stored is None:
            return "duplicate"
        msg, thread = stored
        if quiet:
            return quiet
        _wake(route, parsed, msg, thread)
        return "agent"

    # Redelivery: heal a crash between commit and enqueue.
    if existing.direction != MailMessage.IN or _quiet(route, parsed):
        return "duplicate"
    agent = route.agent
    key = _turn_key(agent, existing.thread, existing)
    if Turn.keyed(key, Turn.scope_for(agent=agent)).exists():
        return "duplicate"
    _wake(route, parsed, existing, existing.thread)
    return "agent"


class SendRefused(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status


MAX_RECIPIENTS = 50
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
        if len(a) > 320 or not a.isascii() or not _ADDR_RE.match(a):
            raise SendRefused(422, f"{what} {_line(raw)[:80]!r} is not an email address")
        out.append(a)
    return out


def _sent_today(domain, workspace_id=None) -> int:
    """Recipients mailed from this domain in the last 24h (to + cc of each outbound message),
    by every workspace using it, or by one when `workspace_id` is given."""
    since = timezone.now() - dt.timedelta(days=1)
    rows = MailMessage.objects.filter(direction=MailMessage.OUT, mailbox__endswith="@" + domain.name,
                                      created_at__gte=since)
    if workspace_id is not None:
        rows = rows.filter(workspace=workspace_id)
    return sum(len(t or []) + len(c or []) + len(b or []) for t, c, b in rows.values_list("to", "cc", "bcc"))


def send(route, *, to, cc=(), bcc=(), subject="", text="", html="", thread=None, attachments=(),
         sender_name="", from_address="", owner=None) -> MailMessage:
    """Send as the route's agent. The From is the route address — or, for a person replying
    from a catch-all, the `from_address` the mail was sent to (the caller proves it resolves
    to this route). `sender_name` is the display name of a person sending; `owner` makes a
    thread this starts private to that person."""
    # The domain row is locked (select_for_update) across the cap check, the SES call and
    # the insert, so concurrent sends from one domain are serialised and cannot overshoot
    # the daily cap. The lock is held during the SES call — acceptable at this volume.
    # A failed send raises inside the transaction, so no MailMessage row is left behind.
    with transaction.atomic():
        domain = MailDomain.objects.select_for_update(of=("self",)).select_related("workspace").get(
            pk=route.domain_id)
        out = _send_locked(route, domain, to=to, cc=cc, bcc=bcc, subject=subject, text=text, html=html,
                           thread=thread, attachments=attachments, sender_name=sender_name,
                           from_address=from_address, owner=owner)
    _enforce_cap(out)  # outside the domain lock: eviction is the mailbox's own business
    _admit_recipients(route, out)
    return out


def attach_uploads(out, uploads) -> None:
    """The files a sent message carried become its attachments, on the keys they already
    have. The message has gone by now, so a problem here is logged, never raised."""
    try:
        with transaction.atomic():
            for u in uploads:
                MailAttachment.objects.create(message=out, filename=u.filename, content_type=u.content_type,
                                              size=u.size, s3_key=u.s3_key, kept=True)
                u.delete()
            total = sum(u.size for u in uploads)
            MailMessage.objects.filter(pk=out.pk).update(size_bytes=F("size_bytes") + total)
    except Exception:  # noqa: BLE001
        logger.exception("mail %s: could not record its attachments", out.pk)


def _admit_recipients(route, out) -> None:
    """Whoever the agent wrote to on a thread is on that thread: the members among
    its To and Cc join the thread's session, the same as the people on an inbound
    message (`email_channel.admit_thread_people`). No proof to check — the agent
    chose them. Never fails the send that called it."""
    if not route.agent_id:
        return
    try:
        key = f"{session_sync.EMAIL_THREAD_PREFIX}{out.thread.uuid}"
        session = (Session.objects.filter(agent_id=route.agent_id,
                                          **{f"metadata__{session_sync.EMAIL_THREAD_KEY}": key})
                   .order_by("created_at").first())
        email_channel.admit_addresses(session, [*(out.to or []), *(out.cc or [])])
    except Exception:  # noqa: BLE001
        logger.exception("mail %s: could not admit the recipients to its session", out.pk)


def _send_locked(route, domain, *, to, cc, bcc, subject, text, html, thread, attachments,
                 sender_name="", from_address="", owner=None) -> MailMessage:
    if domain.status == MailDomain.SUSPENDED:
        raise SendRefused(409, f"sending from {domain.name} is suspended: {domain.suspended_reason}")
    if domain.status != MailDomain.VERIFIED:
        raise SendRefused(409, f"{domain.name} is not verified yet")
    to, cc, bcc = _addresses(to, "recipient"), _addresses(cc, "cc"), _addresses(bcc, "bcc")
    everyone = len(to) + len(cc) + len(bcc)
    if not to:
        raise SendRefused(422, "no recipients")
    own = (from_address or route.address).lower()
    if own in to or own in cc or own in bcc:
        raise SendRefused(422, "an agent cannot send mail to its own address")
    if everyone > MAX_RECIPIENTS:
        raise SendRefused(422, f"at most {MAX_RECIPIENTS} recipients per message")
    share = None
    if route.workspace_id != domain.workspace_id:
        share = domains.share_for(domain, route.workspace)
        if share is None:
            raise SendRefused(409, f"{domain.name} is no longer shared with this workspace")
    if _sent_today(domain) + everyone > domain.daily_send_cap:
        raise SendRefused(429, f"{domain.name} reached its daily send cap ({domain.daily_send_cap})")
    if share is not None and _sent_today(domain, route.workspace_id) + everyone > share.daily_send_cap:
        raise SendRefused(429, f"this workspace reached its daily send cap on {domain.name} ({share.daily_send_cap})")
    subject = _line(subject)
    msg = EmailMessage()
    msg["From"] = formataddr((_line(sender_name) or (_line(route.agent.name) if route.agent_id else ""),
                              from_address or route.address))
    msg["To"] = ", ".join(to)
    if cc:
        msg["Cc"] = ", ".join(cc)
    msg["Date"] = formatdate(localtime=False)
    msg["Message-ID"] = make_msgid(domain=domain.name)
    if thread is not None:
        # The ids the other side saw: for our own sent messages that is SES's Message-ID,
        # not the one canopy wrote (SES replaces it), else their client cannot thread.
        prior = [ses.wire_message_id(sid) if d == MailMessage.OUT and sid else mid
                 for mid, sid, d in thread.messages.order_by("created_at", "pk")
                 .values_list("message_id", "ses_message_id", "direction")]
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
    try:
        ses_id = ses.send_raw(from_addr=from_address or route.address, to=to + cc + bcc, raw=msg.as_bytes())
    except ses.MailOff:
        raise SendRefused(503, "mail is turned off on this deployment") from None
    except ClientError as exc:
        code = exc.response.get("Error", {}).get("Code", "error")
        raise SendRefused(502, f"the mail provider refused: {code}") from None
    except BotoCoreError as exc:
        raise SendRefused(502, f"the mail provider refused: {type(exc).__name__}") from None
    if thread is None:
        thread = MailThread.objects.create(workspace=route.workspace, route=route, owner=owner,
                                           subject=subject[:998])
    out = MailMessage.objects.create(
        thread=thread, workspace=route.workspace, direction=MailMessage.OUT, mailbox=route.address,
        message_id=msg["Message-ID"], in_reply_to=msg.get("In-Reply-To", ""),
        references=msg.get("References", ""), from_addr=from_address or route.address, to=to, cc=cc, bcc=bcc,
        subject=subject[:998], text=text, html=html, ses_message_id=ses_id,
        delivery_status=MailMessage.SENT,
        # The text and html; `attach_uploads` adds the attachments the caller holds in S3.
        size_bytes=storage.message_size(text=text, html=html))
    MailThread.objects.filter(pk=thread.pk).update(
        message_count=F("message_count") + 1, last_message_at=timezone.now(), unread=False)
    return out

"""The two notices a rejection owes: one to the sender, one to the owner.

Both are FIXED TEXT, never written by a model — no session ever ran.

* **Sender** — an automatic reply on their email thread, from the agent's own
  mailbox, only for an email turn, and only when it is safe:
  never to a machine (Auto-Submitted / Precedence / list mail, a no-reply
  address), the agent's own mailbox, a system sender of the workspace, or
  canopy/another agent; and at most once per thread per 24 hours. Off until a
  workspace admin turns it on (`AdmissionSettings`, on the Admission rules
  page): the notice is still recorded, as `held`, so turning it on later
  changes nothing else. A canopy-hosted (SES) thread is answered from the
  thread's own address through the mail app (`agents.mailbox.reply`). On Gmail
  canopy-web sends it
  itself when it can (`gmail.py`); when it cannot — the mailbox's token is on
  the desktop `canopy` client, or Google refuses — the notice is left PENDING
  and the agent's runner sends it on its next mailbox poll
  (`runner_notices.py`). A notice never ends `failed` while a runner could
  still send it.
* **Owner** — separate from the thread, and owner-only: an email (SES,
  `apps/common/email.py`) plus a "Waiting on you" ask on the agent's board,
  both naming who wrote, the rule that fired and why, and links to fix it.

`deliver` runs after the rejection commits and never raises.
"""
from __future__ import annotations

import datetime as dt
import logging
import re
from email.utils import parseaddr

from django.core.mail import EmailMessage
from django.db.models import Q
from django.utils import timezone

from .models import AdmissionNotice, AdmissionRule, AdmissionSettings

logger = logging.getLogger(__name__)

#: How long one thread's sender notice silences the next.
SENDER_WINDOW = dt.timedelta(hours=24)

# A no-reply WORD anywhere in the local part (the domain is never a reason), delimited the way Gmail tokenises it, as the
# runner's inbox rules read it: `comments-noreply@docs.google.com` (a Doc comment) and
# `drive-shares-dm-noreply@google.com` are machines too, not only `noreply@…`.
_NOREPLY = re.compile(
    r"(?:^|[._+-])(?:noreply|no-reply|donotreply|do-not-reply|mailer-daemon|postmaster|bounces?)"
    r"(?=[._+-]|$)", re.I)


def deliver(turn_id, rule_id, *, detail: str = "") -> None:
    from apps.harness.models import Turn

    turn = Turn.objects.filter(pk=turn_id).select_related(
        "agent", "chat_session__agent").first()
    rule = AdmissionRule.objects.filter(pk=rule_id).first()
    if turn is None or rule is None:
        return
    agent = turn.acting_agent
    if agent is None:
        return
    for wanted, kind, send in (
        (rule.notify_sender, "sender notice", lambda: _sender_notice(turn, rule, agent)),
        (rule.notify_owner, "owner notice", lambda: _owner_notice(turn, rule, agent, detail=detail)),
    ):
        if not wanted:
            continue
        try:
            send()
        except Exception:  # noqa: BLE001 — a notice is never worth a crash in the claim path
            logger.exception("admission: %s failed for turn %s", kind, turn_id)


def _record(turn, rule, agent, kind, status, *, recipient="", detail="", thread_id="", **extra):
    return AdmissionNotice.objects.create(
        turn=turn, rule=rule, agent=agent, kind=kind, status=status,
        recipient=recipient[:320], detail=detail, thread_id=thread_id[:64], **extra,
    )


def _owner_name(agent) -> str:
    owner = agent.owner
    if owner is None:
        return "its owner"
    return owner.get_full_name() or owner.email


def sender_text(agent) -> str:
    return (
        f"Hi — this is an automatic reply. {agent.name} can't act on this email on its own "
        f"right now, so it has been passed to {_owner_name(agent)}, {agent.name}'s owner.\n\n"
        f"— {agent.name} (automatic reply)\n"
    )


def _why_not_reply(turn, agent, sender: str) -> str:
    """Why this sender must not get an automatic reply, or ""."""
    from apps.inbound.models import InboundMailbox
    from apps.workspaces.models import SystemSender

    if not sender:
        return "no sender address on the turn"
    if turn.initiator_kind in ("system", "agent"):
        return "the sender is canopy or another agent"
    if _NOREPLY.search(sender.partition("@")[0]):
        return "a no-reply address"
    from apps.agents import mailbox

    own = {a.lower() for a in InboundMailbox.objects.filter(agent=agent).values_list("address", flat=True)}
    own |= {a["address"] for a in mailbox.addresses(agent)}
    if agent.user_id and agent.user.email:
        own.add(agent.user.email.lower())
    if sender in own:
        return "the agent's own mailbox"
    # By address too, not only by how the turn was typed: an agent writing from its own
    # login can be recorded as a member, and it must still never get an automatic reply.
    from apps.agents.models import Agent

    if (Agent.objects.filter(Q(email__iexact=sender) | Q(user__email__iexact=sender)).exists()
            or InboundMailbox.objects.filter(address__iexact=sender).exists()):
        return "the sender is another agent"
    if SystemSender.objects.filter(account__workspace_id=agent.workspace_id, address__iexact=sender).exists():
        return "a system sender of this workspace"
    return ""


def _sender_notice(turn, rule, agent) -> None:
    from apps.harness.models import Turn

    ref = turn.origin_ref or {}
    thread_id = str(ref.get("thread_id") or "")
    sender = parseaddr(str(ref.get("from") or ""))[1].strip().lower()

    def record(status, detail, **extra):
        return _record(turn, rule, agent, AdmissionNotice.SENDER, status,
                       recipient=sender, detail=detail, thread_id=thread_id, **extra)

    if turn.origin != Turn.ORIGIN_EMAIL or not thread_id:
        return record(AdmissionNotice.SKIPPED, "only an email sender gets an automatic reply")
    why = _why_not_reply(turn, agent, sender)
    if why:
        return record(AdmissionNotice.SKIPPED, f"not replied to: {why}")
    recent = AdmissionNotice.objects.filter(
        agent=agent, kind=AdmissionNotice.SENDER, thread_id=thread_id,
        status__in=[AdmissionNotice.SENT, AdmissionNotice.HELD, AdmissionNotice.PENDING],
        created_at__gte=timezone.now() - SENDER_WINDOW,
    ).exclude(turn=turn)
    if recent.exists():
        return record(AdmissionNotice.SKIPPED, "this thread was already answered in the last 24 hours")
    if not AdmissionSettings.sender_notices_on(agent.workspace_id):
        return record(AdmissionNotice.HELD,
                      "automatic replies are off in this workspace (Settings → Admission rules)")
    message_id, subject, body = str(ref.get("message_id") or ""), str(ref.get("subject") or ""), sender_text(agent)
    if ref.get("backend") == "ses":
        # A canopy-hosted thread: `thread_id` is a MailThread uuid, not a Gmail id, so the
        # reply goes out through the mail app from the thread's own address. A refusal
        # sent nothing, and no runner reads this mailbox, so it ends here.
        from apps.agents import mailbox

        try:
            sent_id = mailbox.reply(agent, thread_id=thread_id, to=sender, body=body)
        except mailbox.ReplyRefused as exc:
            return record(AdmissionNotice.FAILED, f"not sent: {exc}")
        except Exception as exc:  # noqa: BLE001
            logger.exception("admission: hosted-mail send failed for turn %s", turn.pk)
            return record(AdmissionNotice.FAILED,
                          f"not known to be sent: the send failed ({type(exc).__name__}) and "
                          "is not retried, so the sender is never answered twice")
        return record(AdmissionNotice.SENT, f"replied on the thread (message {sent_id})")
    from . import gmail

    try:
        sent_id = gmail.send_reply(agent, thread_id=thread_id, message_id=message_id,
                                   to=sender, subject=subject, body=body)
    except gmail.Automated as exc:
        return record(AdmissionNotice.SKIPPED, f"not replied to: {exc}")
    except gmail.Unavailable as exc:
        # Nothing went out, and the agent's runner reads this mailbox with a
        # client it CAN send from: leave the reply to it rather than give up.
        return record(AdmissionNotice.PENDING,
                      f"canopy-web could not send it ({exc}); left for the agent's runner",
                      mailbox=str(ref.get("mailbox") or agent.email or "")[:320],
                      message_id=message_id[:64],
                      subject=subject[:998], body=body)
    except Exception as exc:  # noqa: BLE001
        # The send call itself failed in a way that does not say whether Gmail
        # delivered. Not retried, so the sender is never answered twice.
        logger.exception("admission: Gmail send failed for turn %s", turn.pk)
        return record(AdmissionNotice.FAILED,
                      f"not known to be sent: the Gmail send failed ({type(exc).__name__}) and "
                      "is not retried, so the sender is never answered twice")
    return record(AdmissionNotice.SENT, f"replied on the thread (gmail message {sent_id})")


def fix_links(turn, rule, agent) -> list[dict]:
    from apps.workspaces import services as wsvc

    ws = agent.workspace_id
    links = [
        {"label": "The admission rule that fired",
         "url": wsvc.scoped_url(ws, f"/settings/admission-rules?rule={rule.pk}")},
        {"label": f"{agent.name}'s turn mode and runners",
         "url": wsvc.scoped_url(ws, f"/agents/{agent.slug}/settings#running")},
        {"label": f"Who can reach {agent.name} (interface callers)",
         "url": wsvc.scoped_url(ws, f"/agents/{agent.slug}/settings#reach")},
        {"label": f"The rejected turn ({turn.pk})",
         "url": wsvc.scoped_url(ws, f"/agents/{agent.slug}/turns")},
    ]
    if rule.reach:
        links.insert(1, {"label": "The runners (resume one, or check why it is down)",
                         "url": wsvc.public_url("/supervisor?tab=runners")})
    ref = turn.origin_ref or {}
    thread_id = str(ref.get("thread_id") or "")
    if thread_id and ref.get("backend") == "ses":
        links.append({"label": f"The email thread (in {agent.slug}'s mail)",
                      "url": wsvc.scoped_url(ws, f"/agents/{agent.slug}/mail")})
    elif thread_id:
        links.append({"label": f"The Gmail thread (in {agent.slug}'s mailbox)",
                      "url": f"https://mail.google.com/mail/u/0/#all/{thread_id}"})
    return links


def _reply_line(turn, rule) -> str:
    """What the sender was told, in words: the owner should never have to guess."""
    if not rule.notify_sender:
        return "none: this rule does not reply to the sender"
    notice = AdmissionNotice.objects.filter(turn=turn, kind=AdmissionNotice.SENDER).first()
    if notice is None:
        return "none"
    to = f" to {notice.recipient}" if notice.recipient else ""
    return {
        AdmissionNotice.SENT: f"an automatic reply was sent{to} on the thread",
        AdmissionNotice.PENDING: f"an automatic reply{to} is queued; the agent's runner sends it "
                                 "on its next mailbox check",
        AdmissionNotice.HELD: "none: automatic replies are off in this workspace "
                              "(Settings → Admission rules)",
    }.get(notice.status, f"none: {notice.detail}")


def _owner_body(turn, rule, agent, detail: str = "") -> tuple[str, str]:
    from .rules import summary

    ref = turn.origin_ref or {}
    who = (str(ref.get("from") or "")
           or (turn.initiator_contact.email if turn.initiator_contact_id else "")
           or (turn.initiator_user.email if turn.initiator_user_id else "") or "someone")
    subject = str(ref.get("subject") or "")
    title = f"{agent.name} did not run {turn.origin} from {who}"
    lines = [
        f"{agent.name} received {turn.origin} from {who} and did not run it: an admission rule "
        f"rejected it before any session started.",
        "",
        f"From:    {who}",
    ]
    if subject:
        lines.append(f"Subject: {subject}")
    if ref.get("thread_id"):
        lines.append(f"Thread:  {ref['thread_id']}")
    lines += [
        f"Rule:    {rule.pk}: reject {summary(rule)}",
        f"Why:     {rule.reason}",
        *([f"Runners: {detail}"] if detail else []),
        f"Reply:   {_reply_line(turn, rule)}",
        "",
        "Nothing ran. To handle this one, open the thread yourself. To stop rejecting turns "
        "like it, change the rule, the agent's turn mode, or who may reach it:",
        "",
        *[f"- {link['label']}: {link['url']}" for link in fix_links(turn, rule, agent)],
    ]
    return title, "\n".join(lines)


def _owner_notice(turn, rule, agent, *, detail: str = "") -> None:
    owner = agent.owner
    if owner is None or not owner.email:
        _record(turn, rule, agent, AdmissionNotice.OWNER, AdmissionNotice.SKIPPED,
                detail="the agent has no owner with an email address")
        return
    title, body = _owner_body(turn, rule, agent, detail)
    try:
        status = (AdmissionNotice.SENT if EmailMessage(subject=title, body=body, to=[owner.email]).send()
                  else AdmissionNotice.NOT_CONFIGURED)
        detail = "emailed"
    except Exception as exc:  # noqa: BLE001
        logger.exception("admission: owner email failed for turn %s", turn.pk)
        status, detail = AdmissionNotice.FAILED, f"email failed ({type(exc).__name__})"
    try:
        from apps.agents import services as agent_services
        from apps.workspaces import services as wsvc

        # Parked on the owner, so it lands on their Waiting on you; an owner who
        # is somehow not a member still gets an open ask the agent's admins see.
        waiting = {"waiting_on_email": owner.email} if wsvc.is_member(owner, agent.workspace_id) else {}
        task = agent_services.create_tasks(agent, [{
            "title": title[:200],
            "ask_kind": "review",
            "ask_body": body,
            "origin": turn.origin,
            "origin_ref": {"kind": "admission_rejected", "turn_id": str(turn.pk), "rule_id": rule.pk},
            "links": fix_links(turn, rule, agent),
            **waiting,
            "idempotency_key": f"admission:{turn.pk}",
        }])[0]
        detail += f"; board ask {task.pk}"
    except Exception as exc:  # noqa: BLE001
        logger.exception("admission: owner board ask failed for turn %s", turn.pk)
        detail += f"; board ask failed ({type(exc).__name__})"
    _record(turn, rule, agent, AdmissionNotice.OWNER, status, recipient=owner.email, detail=detail)

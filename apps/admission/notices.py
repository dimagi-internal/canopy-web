"""The two notices a rejection owes: one to the sender, one to the owner.

Both are FIXED TEXT, never written by a model — no session ever ran.

* **Sender** — an automatic reply on their email thread, from the agent's own
  mailbox (`gmail.py`), only for an email turn, and only when it is safe:
  never to a machine (Auto-Submitted / Precedence / list mail, a no-reply
  address), the agent's own mailbox, a system sender of the workspace, or
  canopy/another agent; and at most once per thread per 24 hours. Off unless
  `CANOPY_ADMISSION_SENDER_NOTICES` is set: the notice is still recorded, as
  `held`, so turning it on later changes nothing else.
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

from django.conf import settings
from django.core.mail import EmailMessage
from django.utils import timezone

from .models import AdmissionNotice, AdmissionRule

logger = logging.getLogger(__name__)

#: How long one thread's sender notice silences the next.
SENDER_WINDOW = dt.timedelta(hours=24)

_NOREPLY = re.compile(
    r"^(?:noreply|no-reply|donotreply|do-not-reply|mailer-daemon|postmaster|bounces?)[@+.-]", re.I)


def deliver(turn_id, rule_id) -> None:
    from apps.harness.models import Turn

    turn = Turn.objects.filter(pk=turn_id).select_related(
        "agent", "chat_session__agent").first()
    rule = AdmissionRule.objects.filter(pk=rule_id).first()
    if turn is None or rule is None:
        return
    from .evaluate import _agent_of

    agent = _agent_of(turn)
    if agent is None:
        return
    for wanted, fn in ((rule.notify_sender, _sender_notice), (rule.notify_owner, _owner_notice)):
        if not wanted:
            continue
        try:
            fn(turn, rule, agent)
        except Exception:  # noqa: BLE001 — a notice is never worth a crash in the claim path
            logger.exception("admission: %s failed for turn %s", fn.__name__, turn_id)


def _record(turn, rule, agent, kind, status, *, recipient="", detail="", thread_id=""):
    return AdmissionNotice.objects.create(
        turn=turn, rule=rule, agent=agent, kind=kind, status=status,
        recipient=recipient[:320], detail=detail, thread_id=thread_id[:64],
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
    if _NOREPLY.match(sender):
        return "a no-reply address"
    own = {a.lower() for a in InboundMailbox.objects.filter(agent=agent).values_list("address", flat=True)}
    if agent.user_id and agent.user.email:
        own.add(agent.user.email.lower())
    if sender in own:
        return "the agent's own mailbox"
    if SystemSender.objects.filter(account__workspace_id=agent.workspace_id, address__iexact=sender).exists():
        return "a system sender of this workspace"
    return ""


def _sender_notice(turn, rule, agent) -> None:
    from apps.harness.models import Turn

    ref = turn.origin_ref or {}
    thread_id = str(ref.get("thread_id") or "")
    sender = parseaddr(str(ref.get("from") or ""))[1].strip().lower()

    def record(status, detail):
        return _record(turn, rule, agent, AdmissionNotice.SENDER, status,
                       recipient=sender, detail=detail, thread_id=thread_id)

    if turn.origin != Turn.ORIGIN_EMAIL or not thread_id:
        return record(AdmissionNotice.SKIPPED, "only an email sender gets an automatic reply")
    why = _why_not_reply(turn, agent, sender)
    if why:
        return record(AdmissionNotice.SKIPPED, f"not replied to: {why}")
    recent = AdmissionNotice.objects.filter(
        agent=agent, kind=AdmissionNotice.SENDER, thread_id=thread_id,
        status__in=[AdmissionNotice.SENT, AdmissionNotice.HELD],
        created_at__gte=timezone.now() - SENDER_WINDOW,
    ).exclude(turn=turn)
    if recent.exists():
        return record(AdmissionNotice.SKIPPED, "this thread was already answered in the last 24 hours")
    if not getattr(settings, "CANOPY_ADMISSION_SENDER_NOTICES", False):
        return record(AdmissionNotice.HELD,
                      "automatic replies are off on this deployment (CANOPY_ADMISSION_SENDER_NOTICES)")
    from . import gmail

    try:
        sent_id = gmail.send_reply(
            agent, thread_id=thread_id, message_id=str(ref.get("message_id") or ""),
            to=sender, subject=str(ref.get("subject") or ""), body=sender_text(agent),
        )
    except gmail.Automated as exc:
        return record(AdmissionNotice.SKIPPED, f"not replied to: {exc}")
    except gmail.Unavailable as exc:
        return record(AdmissionNotice.FAILED, f"not sent: {exc}")
    except Exception as exc:  # noqa: BLE001
        logger.exception("admission: Gmail send failed for turn %s", turn.pk)
        return record(AdmissionNotice.FAILED, f"not sent: Gmail refused ({type(exc).__name__})")
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
    thread_id = str((turn.origin_ref or {}).get("thread_id") or "")
    if thread_id:
        links.append({"label": f"The Gmail thread (in {agent.slug}'s mailbox)",
                      "url": f"https://mail.google.com/mail/u/0/#all/{thread_id}"})
    return links


def _owner_body(turn, rule, agent) -> tuple[str, str]:
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
        "",
        "Nothing ran. To handle this one, open the thread yourself. To stop rejecting turns "
        "like it, change the rule, the agent's turn mode, or who may reach it:",
        "",
        *[f"- {link['label']}: {link['url']}" for link in fix_links(turn, rule, agent)],
    ]
    return title, "\n".join(lines)


def _owner_notice(turn, rule, agent) -> None:
    owner = agent.owner
    if owner is None or not owner.email:
        _record(turn, rule, agent, AdmissionNotice.OWNER, AdmissionNotice.SKIPPED,
                detail="the agent has no owner with an email address")
        return
    title, body = _owner_body(turn, rule, agent)
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

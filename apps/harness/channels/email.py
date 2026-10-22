"""Email as a door to a turn: who wrote in, whether they are refused at the
door, and which conversation (session) the mail belongs to.

`services.enqueue_turn` asks `arrive` for every email turn with an agent, before
any generic work, and `log_unproven_member` once the turn exists. Nothing here
grants anything: an inbound email is an unauthenticated, spoofable identity, and
the recorded contact only feeds later turns and routing
(docs/architecture/access.md, docs/architecture/mail.md).
"""
from __future__ import annotations

import logging
from dataclasses import dataclass

from django.conf import settings
from django.db import IntegrityError, transaction
from django.utils import timezone

from apps.agents.models import SenderTrust
from apps.contacts import services as contacts
from apps.contacts.models import Contact
from apps.workspaces import services as wsvc

from .. import initiator as who
from ..ledger import append_events
from ..models import Turn

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class EmailArrival:
    """What an email turn is, once its sender and thread are known: the target
    (`agent`, or `session` with agent None once bound to the thread), the
    `initiator`, and — for a blocked sender — the `(turn, created)` already
    written CANCELLED, which is the whole answer."""

    agent: object
    session: object
    initiator: object
    refusal: tuple | None = None


def arrive(agent, origin_ref, *, initiator=None, session=None, authserv_id=None,
           origin: str, idempotency_key: str, prompt: str, routing: str) -> EmailArrival:
    """Resolve an email turn's asker and conversation before it is enqueued."""
    from .. import services as harness  # cycle: services calls arrive(); email_thread_session lives there

    # Record WHO wrote in, before the turn is bound to a session and the
    # agent reference is handed over below. Best-effort and deliberately
    # non-fatal: a contact is a convenience for later turns and for routing,
    # and mail must still be answered when it cannot be recorded.
    #
    # This GRANTS NOTHING — see apps.contacts.models. It is the one thing to
    # keep true here, because an inbound email is an unauthenticated,
    # spoofable identity and "a message creates a member" is exactly the
    # pattern this codebase spent a release removing from five endpoints.
    try:
        email_contact = record_email_contact(agent, origin_ref, authserv_id=authserv_id)
    except Exception:  # noqa: BLE001
        email_contact = None
        logger.exception("could not record the sender of an email turn")
    if email_contact is not None and email_contact.is_blocked:
        # Refused at the door, and SAID so: the turn is written already
        # CANCELLED rather than not written. Three reasons. It keeps the
        # idempotency key, so the runner's next poll of the same unread
        # thread is a no-op instead of a fresh refusal. It needs no protocol
        # change — a runner older than this sees an ordinary 201, where a
        # 4xx would abort its whole mailbox poll and stall everyone else's
        # mail behind one blocked sender. And the refusal is visible in the
        # turn log, where "why did the agent never answer X" gets asked.
        # No session: a blocked person's thread is not a conversation.
        return EmailArrival(agent, session, initiator, refusal=refused_email_turn(
            agent, email_contact, origin=origin,
            idempotency_key=idempotency_key, prompt=prompt,
            origin_ref=origin_ref, routing=routing,
        ))
    if initiator is None:
        # The one door whose asker canopy can know on its own. NOT
        # `enqueued_by`: on an email turn that is the runner's account, and
        # recording it as the initiator is exactly the confusion this field
        # exists to end. No recordable sender -> unknown, honestly.
        member = member_behind_email(
            agent, email_contact, str((origin_ref or {}).get("subject") or ""))
        if member is not None:
            # A MEMBER of the agent's workspace, proven by THIS message's DMARC
            # alignment. The contact rides along so its profile still reaches
            # the agent; the person is who the turn acts for.
            initiator = who.Initiator(who.USER, "email", email_contact.last_auth_result,
                                      user=member, contact=email_contact)
        else:
            initiator = (who.for_contact(email_contact, via="email") if email_contact
                         else who.unknown(via="email"))

    if session is None:
        thread_id = str((origin_ref or {}).get("thread_id") or "")
        if thread_id:
            try:
                session = harness.email_thread_session(
                    agent, thread_id, str((origin_ref or {}).get("subject") or ""))
                agent = None          # the session now carries the agent
            except Exception:  # noqa: BLE001
                # Degrade to the previous behaviour. A turn that runs without a
                # session is the status quo; a turn that fails to EXIST because
                # its session could not be made is a regression, and mail is not
                # a surface that tolerates one.
                logger.exception("could not bind an email turn to a session")
                session = None
    return EmailArrival(agent, session, initiator)


def record_email_contact(agent, origin_ref, *, authserv_id: str | None = None):
    """Upsert a Contact for an email turn's sender, in the AGENT's workspace.

    The agent's tenant, not the enqueuer's: `Turn.enqueued_by` on an email turn
    is the runner's own account (see `apps.harness.actors`), so keying the
    contact off the caller would file every correspondent under the runner
    owner. The sender is in `origin_ref["from"]`, written by the inbox watcher.

    The grade comes from `origin_ref["headers"]` — the newest message's
    `Authentication-Results` headers, shipped by the runner — read against
    `settings.INBOUND_EMAIL_AUTHSERV_ID`. WHICH receiver is trusted is canopy's
    decision, never the poster's: an `authserv_id` or a pre-digested
    `authentication_results` string in the payload is ignored, because either
    would let whoever posts the turn write the verdict on their own mail. A turn
    with no headers (a runner older than this) is graded `none`, honestly.
    """
    ref = origin_ref if isinstance(origin_ref, dict) else {}
    sender = str(ref.get("from") or "")
    if not sender:
        return None
    headers = ref.get("headers")
    # Returned so the turn can name its initiator (`apps/harness/initiator.py`):
    # the person who wrote in, not the runner that posted the turn.
    return contacts.record_inbound_sender(
        workspace=agent.workspace,
        address=sender,
        display_name=str(ref.get("from_name") or ""),
        headers=headers if isinstance(headers, list) else None,
        authserv_id=authserv_id or settings.INBOUND_EMAIL_AUTHSERV_ID,
    )


def address_holder(email: str, workspace_id: str):
    """`(user, role)` for the canopy user who provably holds `email`, and their
    role in `workspace_id` — `(None, None)` when no single user holds it, and
    `(user, None)` when one does but is not a member.

    The lookup `member_behind_email` links a sender with, shared with the caller
    envelope's `unproven_member` note so the two cannot drift. It says WHO an
    address belongs to and nothing about whether a given message proves it:
    answering that (this message's alignment) is each caller's own gate, and
    finding a user here grants nothing.
    """
    user = contacts.user_for_verified_email(email)
    if user is None:
        return None, None
    return user, wsvc.member_role(user, workspace_id)


def member_behind_email(agent, contact, subject: str = ""):
    """The canopy user who sent this email, when that can be PROVEN — else None.

    Why: an email sender is otherwise always a contact, so once an agent
    publishes a declared interface, its own staff writing in ("resume the run")
    would be confined as callers. The who-is-asking arrival rule (§2, D1) —
    existing accounts only, never created — applied to mail. All of:

      * THIS message is aligned (DMARC, or DKIM signed BY the From: domain) on our
        own receiver's verdict. SPF or unaligned DKIM
        alone do not tie the signature to the visible From; the best-ever grade
        says nothing about a spoof today.
      * exactly one canopy user provably holds that address
        (`contacts.services.user_for_verified_email`: a VERIFIED allauth email,
        or — with none — an agent's own login, `Agent.user`).
      * that user is a member of the agent's workspace. A canopy account with
        no business in this tenant stays a contact.
      * the contact is not already linked to someone else.

    On success the contact is linked (`promote_to_user`), which grants nothing.

    The one exception to the alignment rule is a TEMPORARY `SenderTrust` an
    agent admin set for this exact address (`trusted_unaligned_sender`): a
    DKIM-signed but unaligned message from it passes the first check. Every other
    check still applies, and the contact is not linked.

    Failing that, a SYSTEM ACCOUNT of the agent's workspace bound to this
    address and subject (`workspaces.system_accounts.account_for_inbound`) —
    the same alignment requirement, and the binding is per workspace, because
    an automated address is shared: `no-reply@sns.amazonaws.com` sends every
    AWS customer's alarms. Its contact is NOT linked to it: the contact is the
    shared address, and linking it would make the binding look like proof of
    the address rather than a workspace's decision about some of its mail.
    """
    if contact is None or not contact.email:
        return None
    trust = None
    if contact.last_auth_result not in Contact.EMAIL_ALIGNED:
        trust = trusted_unaligned_sender(agent, contact)
        if trust is None:
            return None
    user, role = address_holder(contact.email, agent.workspace_id)
    if user is None and trust is not None:
        # A trust rule names a MEMBER's address; it never reaches system accounts.
        return None
    if user is None:
        from apps.workspaces.system_accounts import account_for_inbound  # cycle: system_accounts reaches the harness

        account = account_for_inbound(agent.workspace_id, contact.email, subject)
        # Still a member: removing it from the workspace is how an admin who
        # does not know about `disabled` switches it off.
        if account is None or not wsvc.is_member(account.user, agent.workspace_id):
            return None
        return account.user
    if contact.user_id is not None and contact.user_id != user.pk:
        return None
    # A question about the SENDER's membership, asked through the one authorizer
    # (`address_holder` reads the role with `wsvc.member_role`).
    if role is None:
        return None
    # Linking is for proven mail only: a temporary exception must not leave a
    # permanent link behind when it expires.
    if contact.user_id is None and trust is None:
        contacts.promote_to_user(contact, user)
    return user


def trusted_unaligned_sender(agent, contact):
    """The `SenderTrust` that lets THIS unaligned message stand in for alignment,
    or None. Needs an unexpired rule for this agent and exact address AND a
    passing DKIM signature on the message (tier 2): the rule excuses whose key
    signed, never the absence of a signature. See `agents.models.SenderTrust`."""
    grade = contact.last_auth_result or Contact.AUTH_NONE
    if Contact.AUTH_RANK.get(grade, 0) < Contact.AUTH_RANK[Contact.TIER_SIGNED]:
        return None
    return SenderTrust.active_for(agent.pk, contact.email)


def log_unproven_member(turn) -> None:
    """Record the envelope's `unproven_member` (and `sender_trust`) on the turn's
    event ledger.

    Where an owner already reads a turn's history (`read_turn_events`, the
    activity drill-down): "a member wrote in, but this message could not be tied
    to their account, so they were treated as a contact" (canopy-web#1265).
    Logging only — it grants nothing, and the turn's initiator, profile and
    grant are already decided by the time this runs. Never raises: a log line
    is not worth failing to enqueue someone's mail.
    """
    from .. import caller_context  # cycle: caller_context imports address_holder from here

    try:
        note = caller_context.unproven_member(turn)
        if note is not None:
            append_events(turn, [{"kind": caller_context.UNPROVEN_MEMBER_EVENT, "payload": note}])
        trust = caller_context.sender_trust(turn)
        if trust is not None:
            append_events(turn, [{"kind": caller_context.SENDER_TRUST_EVENT, "payload": trust}])
    except Exception:  # noqa: BLE001
        logger.exception("could not log the sender's standing for turn %s", turn.pk)


def refused_email_turn(agent, contact, *, origin, idempotency_key, prompt,
                        origin_ref, routing) -> tuple[Turn, bool]:
    """Write an email turn from a BLOCKED contact as already cancelled."""
    scope = Turn.scope_for(agent=agent)
    existing = Turn.keyed(idempotency_key, scope).first()
    if existing is not None:
        return replay_email(existing, agent), False
    reason = contact.blocked_reason or "no reason given"
    try:
        with transaction.atomic():
            turn = Turn.objects.create(
                **who.for_contact(contact, via="email").fields(),
                agent=agent,
                origin=origin,
                idempotency_key=idempotency_key,
                prompt=prompt,
                origin_ref=origin_ref or {},
                routing=routing,
                status=Turn.CANCELLED,
                finished_at=timezone.now(),
                result_note=f"not run: the sender is blocked ({reason})",
            )
    except IntegrityError:
        replay = Turn.keyed(idempotency_key, scope).first()
        if replay is not None:
            return replay_email(replay, agent), False
        raise
    logger.info("email turn %s refused: contact %s is blocked", turn.pk, contact.pk)
    # Refused whatever the rules say; the workspace's `blocked-sender` rule
    # decides what is recorded and who is told (canopy-web#1416).
    from apps.admission import evaluate as admission

    admission.at_refusal(turn, sender="blocked")
    return turn, True


def replay_email(existing: Turn, agent) -> Turn:
    # The same mail may have become a session turn before its sender was
    # blocked, so the replay is honoured for any turn this agent acts in.
    acting = existing.acting_agent
    if acting is None or acting.pk != agent.pk:
        from ..services import IdempotencyConflict  # cycle: services calls arrive()

        raise IdempotencyConflict()
    return existing

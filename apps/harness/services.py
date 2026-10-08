"""Harness domain services — the only write path for Runner/Turn/TurnEvent.

What still lives here is a turn's lifecycle around the claim — enqueue (and the
email and capability gates on the way in), heartbeat, mark running, finish,
cancel — and the runner's session reports. Leases are renewed by runner
heartbeats and swept lazily on claim. All functions are synchronous and
transaction-safe.

The rest is split by responsibility, and re-exported below: routing (`routing`),
the claim and its explanations (`claim`), the event ledger and transcripts
(`ledger`), scheduled turns (`schedule_turns`), runner administration
(`runners`) and readiness drills (`drills`).
"""
from __future__ import annotations

import datetime as dt
import logging
import re
import uuid
from dataclasses import dataclass

from django.conf import settings
from django.db import IntegrityError, transaction
from django.db.models import F, Q
from django.utils import timezone

from apps.canopy_sessions.staleness import stale_cutoff
from apps.workspaces import services as wsvc

# HEARTBEAT_ONLINE_WINDOW lives on models.py (Runner.live_status uses it too;
# models.py cannot import services.py, which already imports models.py) and is
# re-exported here so existing importers of services.HEARTBEAT_ONLINE_WINDOW keep
# working. Intentional re-export — noqa keeps the F401 gate from deleting it.
from .models import (
    HEARTBEAT_ONLINE_WINDOW,  # noqa: F401
    Runner,
    RunnerDrill,
    Turn,
)

# The harness was one ~4,000-line module; these are the pieces it was split
# into (2026-10-06). Every name each one defines is re-exported here, public and
# private alike, so `services.X` and `from apps.harness.services import X` keep
# resolving exactly as before. Patching `services.X` reaches only the code that
# still LIVES here — to patch what a moved function calls, patch its own module.
from .routing import (
    _ancestor_chains,  # noqa: F401
    _assignment_allows,  # noqa: F401
    _assignment_allows_for_agent,  # noqa: F401
    _dialog_up,  # noqa: F401
    _is_repo_turn,  # noqa: F401
    _kind_allows,  # noqa: F401
    _repo_order_allows,  # noqa: F401
    agent_tenant_q,  # noqa: F401
    agents_following_runner,  # noqa: F401
    agents_served_by,  # noqa: F401
    agents_with_own_order,  # noqa: F401
    assignment_rows_for,  # noqa: F401
    blocking_turn,  # noqa: F401
    busy_agent_ids,  # noqa: F401
    busy_session_ids,  # noqa: F401
    CASCADE_GRACE_SECONDS,  # noqa: F401
    default_order_source,  # noqa: F401
    delivers_midturn,  # noqa: F401
    ENVELOPE_VERSION,  # noqa: F401
    EXECUTING,  # noqa: F401
    inherited_orders,  # noqa: F401
    InheritedOrder,  # noqa: F401
    load_assignment_rows,  # noqa: F401
    load_workspace_orders,  # noqa: F401
    may_ride,  # noqa: F401
    MIDTURN_FAILED,  # noqa: F401
    profile_q,  # noqa: F401
    PROFILES_VERSION,  # noqa: F401
    repo_order_for,  # noqa: F401
    ridable_sessions,  # noqa: F401
    runner_target_q,  # noqa: F401
    runner_tenant_slugs,  # noqa: F401
    seed_assignments_from_capabilities,  # noqa: F401
)
from .ledger import (
    append_events,  # noqa: F401
    append_transcript,  # noqa: F401
    iter_transcript,  # noqa: F401
    iter_transcript_lines,  # noqa: F401
    read_transcript,  # noqa: F401
    transcript_messages,  # noqa: F401
    TRANSCRIPT_TURN_MAX_BYTES,  # noqa: F401
    TRANSCRIPT_VIEW_MAX_MESSAGES,  # noqa: F401
)
from .schedule_turns import (
    _occurrences,  # noqa: F401
    _raise_schedule_nag,  # noqa: F401
    _schedule_initiator,  # noqa: F401
    fire_schedule,  # noqa: F401
    LATE_SLOT_WINDOW_MINUTES,  # noqa: F401
    latest_occurrence_turn,  # noqa: F401
    OneOffSlotMismatch,  # noqa: F401
    release_stale_occurrence_turns,  # noqa: F401
    release_stale_occurrence_turns_all,  # noqa: F401
    resolve_schedule_nags,  # noqa: F401
    run_schedule_now,  # noqa: F401
    skip_late_scheduled_turns,  # noqa: F401
    supersede_open_turns,  # noqa: F401
)
from .claim import (
    _assignment_rows_for_turns,  # noqa: F401
    _coverage,  # noqa: F401
    _readable_session_turn_q,  # noqa: F401
    _refined_allows,  # noqa: F401
    _routing_basis,  # noqa: F401
    claim_next_turn,  # noqa: F401
    DEFAULT_LEASE_SECONDS,  # noqa: F401
    LIVE,  # noqa: F401
    OFFLINE,  # noqa: F401
    Reach,  # noqa: F401
    sweep_expired_leases,  # noqa: F401
    turn_reach,  # noqa: F401
    UNCLAIMABLE_GRACE,  # noqa: F401
    unclaimable_queued_turns,  # noqa: F401
    UNROUTED,  # noqa: F401
)
from .runners import (
    _expire_if_stale,  # noqa: F401
    can_administer_runner,  # noqa: F401
    claim_runner_mint,  # noqa: F401
    current_runner_mint,  # noqa: F401
    finish_runner_mint,  # noqa: F401
    get_runner_credential,  # noqa: F401
    grant_runner_admin,  # noqa: F401
    list_runner_admins,  # noqa: F401
    MINT_TTL_SECONDS,  # noqa: F401
    record_mint_url,  # noqa: F401
    request_refresh,  # noqa: F401
    revoke_runner_admin,  # noqa: F401
    runner_credential_status,  # noqa: F401
    set_runner_credential,  # noqa: F401
    set_runner_flags,  # noqa: F401
    start_runner_mint,  # noqa: F401
    submit_mint_code,  # noqa: F401
    swap_runner_logins,  # noqa: F401
    take_mint_code,  # noqa: F401
)
from .drills import (
    _drill_github_check,  # noqa: F401
    _drill_initiator,  # noqa: F401
    _DRILL_REPORT_MAX_AGE,  # noqa: F401
    _DRILL_REPORT_SALT,  # noqa: F401
    DRILL_PROMPT,  # noqa: F401
    drill_report_token,  # noqa: F401
    drill_report_token_ok,  # noqa: F401
    report_drill,  # noqa: F401
    start_drill,  # noqa: F401
)

logger = logging.getLogger(__name__)


# How many times a turn that died BEFORE its session existed goes back on the queue
# before we give up and let it stay FAILED. Three is enough to ride out an emdash
# restart or a laptop waking up mid-drain; past that the fault is not transient and a
# turn that keeps churning is worse than one that stops and stays visible.
MAX_SESSIONLESS_RETRIES = 3


def _record_email_contact(agent, origin_ref):
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
    from apps.contacts import services as contacts

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
        authserv_id=settings.INBOUND_EMAIL_AUTHSERV_ID,
    )


def address_holder(email: str, workspace_id: str):
    """`(user, role)` for the canopy user who provably holds `email`, and their
    role in `workspace_id` — `(None, None)` when no single user holds it, and
    `(user, None)` when one does but is not a member.

    The lookup `_member_behind_email` links a sender with, shared with the caller
    envelope's `unproven_member` note so the two cannot drift. It says WHO an
    address belongs to and nothing about whether a given message proves it:
    answering that (this message's alignment) is each caller's own gate, and
    finding a user here grants nothing.
    """
    from apps.contacts import services as contacts

    user = contacts.user_for_verified_email(email)
    if user is None:
        return None, None
    return user, wsvc.member_role(user, workspace_id)


def _member_behind_email(agent, contact, subject: str = ""):
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

    Failing that, a SYSTEM ACCOUNT of the agent's workspace bound to this
    address and subject (`workspaces.system_accounts.account_for_inbound`) —
    the same alignment requirement, and the binding is per workspace, because
    an automated address is shared: `no-reply@sns.amazonaws.com` sends every
    AWS customer's alarms. Its contact is NOT linked to it: the contact is the
    shared address, and linking it would make the binding look like proof of
    the address rather than a workspace's decision about some of its mail.
    """
    from apps.contacts import services as contacts
    from apps.contacts.models import Contact

    if contact is None or contact.last_auth_result not in Contact.EMAIL_ALIGNED or not contact.email:
        return None
    user, role = address_holder(contact.email, agent.workspace_id)
    if user is None:
        from apps.workspaces.system_accounts import account_for_inbound

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
    if contact.user_id is None:
        contacts.promote_to_user(contact, user)
    return user


def _log_unproven_member(turn) -> None:
    """Record the envelope's `unproven_member` on the turn's event ledger.

    Where an owner already reads a turn's history (`read_turn_events`, the
    activity drill-down): "a member wrote in, but this message could not be tied
    to their account, so they were treated as a contact" (canopy-web#1265).
    Logging only — it grants nothing, and the turn's initiator, profile and
    grant are already decided by the time this runs. Never raises: a log line
    is not worth failing to enqueue someone's mail.
    """
    from . import caller_context

    try:
        note = caller_context.unproven_member(turn)
        if note is not None:
            append_events(turn, [{"kind": caller_context.UNPROVEN_MEMBER_EVENT, "payload": note}])
    except Exception:  # noqa: BLE001
        logger.exception("could not log unproven_member for turn %s", turn.pk)


def _refused_email_turn(agent, contact, *, origin, idempotency_key, prompt,
                        origin_ref, routing) -> tuple[Turn, bool]:
    """Write an email turn from a BLOCKED contact as already cancelled."""
    from . import initiator as who

    existing = Turn.objects.filter(idempotency_key=idempotency_key).first()
    if existing is not None:
        return existing, False
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
        replay = Turn.objects.filter(idempotency_key=idempotency_key).first()
        if replay is not None:
            return replay, False
        raise
    logger.info("email turn %s refused: contact %s is blocked", turn.pk, contact.pk)
    return turn, True


def _apply_capability(turn: Turn, requested: str | None = None) -> None:
    """Decide which profile a new turn runs in, inside the transaction that
    creates it — so no runner can claim it before the decision is recorded.

    THE rule, `apps.agents.access.decide` (docs/architecture/access.md): FULL
    for its owner, admins, canopy's own turns and a workspace editor (whose turn
    then runs manual — `turn_mode.editor_cap`); a viewer or a contact gets the
    capability their class is offered, or a `full:` rule's whole profile. Anyone
    offered nothing is refused: the turn is written already CANCELLED with the
    reason and what they CAN do, for the same reasons a blocked sender's is (the
    idempotency key holds, an old runner sees a 201, and the refusal is visible
    in the turn log). Every door that makes an agent work — the harness API, a
    chat send, Slack, email — comes through here.
    """
    from apps.agents import access

    agent = turn.agent if turn.agent_id else (
        turn.chat_session.agent if turn.chat_session_id and turn.chat_session.agent_id else None)
    if agent is None:
        return
    decision = access.decide_for_turn(turn, agent, requested)
    cap = decision.stamp
    if cap == "":
        return
    if cap is None:
        turn.status = Turn.CANCELLED
        turn.finished_at = timezone.now()
        turn.result_note = f"not run: {decision.reason or agent.slug + ' does not take work from you'}"
        turn.save(update_fields=["status", "finished_at", "result_note"])
        return
    turn.capability = cap
    turn.save(update_fields=["capability"])


def enqueue_turn(
    *,
    agent=None,
    project: str = "",
    session=None,
    workspace=None,
    origin: str,
    idempotency_key: str,
    prompt: str = "",
    origin_ref: dict | None = None,
    routing: str = Turn.PREFER_LOCAL,
    enqueued_by=None,
    pinned_runner=None,
    initiator=None,
    capability: str | None = None,
    requested_turn_mode: str = "",
    requested_turn_mode_by=None,
    parent=None,
    provenance_extra: dict | None = None,
) -> tuple[Turn, bool]:
    """Queued turns stack freely — the executing-turn index never blocks intake
    (new turns are born `queued`, which the index does not cover).

    Targets exactly one of agent / project / session. A project turn must carry a
    workspace: it has no agent/session to derive tenancy from, and claim_next_turn
    fails it closed without one, so accepting it here would silently queue a turn
    nothing can ever run. Session turns derive tenancy from session.workspace.

    `parent` names what this turn was started FROM ({turn, session, task, host,
    project, claude_session}: ids, or a Turn/Session) — a payload's `parent`, or
    canopy's own (a transfer, a re-ask, a dispatch). It overrides the request's
    X-Canopy-Parent-* headers key by key. `provenance_extra` adds keys to the
    turn's provenance record (`clicked_by`). See apps/harness/provenance.py.
    """
    # One chokepoint for the retired spellings, because not every producer comes
    # through a request schema: TurnSpec.from_dict parses origin as a free string
    # out of Item JSON written before this deploy. The input schemas normalize too
    # (so a caller gets a clean 422 on a genuinely unknown value rather than a
    # silent rewrite); this is the leg that catches stored payloads and internal
    # callers. Remove with the aliases, one release on.
    origin = Turn.LEGACY_ORIGIN_ALIASES.get(origin, origin)
    # An email thread IS a conversation, so it targets a SESSION rather than the
    # agent directly. A Turn is a fine unit of execution and a poor unit of
    # conversation: it ends, and leaves a human nothing to open, watch or type
    # into. A Session is what ace-web already renders through the shared canopy
    # chat kit, so this is what turns "ACE replied to that eventually" into
    # "here is the run, live, and you can steer it".
    #
    # A CONVERSION, not an addition, and not by choice: `turn_targets_agent_xor
    # _project_xor_session` is a database CHECK constraint, so a turn carrying
    # both an agent and a session cannot be written at all. Found by running the
    # tests — reading the service function suggested the opposite.
    #
    # What moves, deliberately:
    #   * serialization is per SESSION, not per agent, so two email threads to
    #     the same agent now run in parallel — the behaviour a reader expects of
    #     separate conversations, and the reason to key on the thread at all;
    #   * tenancy derives from session.workspace, which is set to the agent's own.
    # What does NOT move, verified: routing resolves a session turn's agent via
    # `chat_session.agent_id` (claim_next_turn's agent_ids is a union of both
    # legs), so the source/actor rules still apply; `resolve_agent_slug` already
    # surfaces `chat_session.agent.slug`, so the runner drives the same agent in
    # the same clone; and every runner in the fleet declares sessions=true, so
    # nothing is stranded on one box.
    if origin == Turn.ORIGIN_EMAIL and agent is not None:
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
            email_contact = _record_email_contact(agent, origin_ref)
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
            return _refused_email_turn(
                agent, email_contact, origin=origin,
                idempotency_key=idempotency_key, prompt=prompt,
                origin_ref=origin_ref, routing=routing,
            )
        if initiator is None:
            # The one channel whose asker enqueue_turn can know on its own. NOT
            # `enqueued_by`: on an email turn that is the runner's account, and
            # recording it as the initiator is exactly the confusion this field
            # exists to end. No recordable sender -> unknown, honestly.
            from . import initiator as who
            member = _member_behind_email(
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

    if session is None and agent is not None and origin == Turn.ORIGIN_EMAIL:
        thread_id = str((origin_ref or {}).get("thread_id") or "")
        if thread_id:
            try:
                session = email_thread_session(
                    agent, thread_id, str((origin_ref or {}).get("subject") or ""))
                agent = None          # the session now carries the agent
            except Exception:  # noqa: BLE001
                # Degrade to the previous behaviour. A turn that runs without a
                # session is the status quo; a turn that fails to EXIST because
                # its session could not be made is a regression, and mail is not
                # a surface that tolerates one.
                logger.exception("could not bind an email turn to a session")
                session = None
    if sum([bool(agent), bool(project), bool(session)]) != 1:
        raise ValueError("a turn targets exactly one of agent / project / session")
    if project and workspace is None:
        raise ValueError("a project turn needs a workspace")
    existing = Turn.objects.filter(idempotency_key=idempotency_key).first()
    if existing is not None:
        return existing, False
    if initiator is None:
        # A caller that says nothing gets `unknown`, not a guess from
        # `enqueued_by`: that field is the CALLER, which for a runner-posted turn
        # is the wrong person. `tests/test_turn_initiator.py` fails on any
        # production path that still lands here.
        from . import initiator as who
        initiator = who.unknown(via=origin)
    from . import provenance

    try:
        with transaction.atomic():
            turn = Turn.objects.create(
                **initiator.fields(),
                **provenance.turn_fields(parent=parent, **(provenance_extra or {})),
                agent=agent,
                project=project,
                chat_session=session,
                # Agent + session turns derive tenancy (agent.workspace /
                # chat_session.workspace) and must not denormalize a second copy
                # that can drift; only a project turn carries its own workspace FK.
                workspace=workspace if project else None,
                origin=origin,
                idempotency_key=idempotency_key,
                prompt=prompt,
                origin_ref=origin_ref or {},
                routing=routing,
                enqueued_by=enqueued_by if getattr(enqueued_by, "is_authenticated", False) else None,
                pinned_runner=pinned_runner,
                # Authorized by the caller (the API view); only meaningful on an
                # agent turn, which is the only kind the view lets carry one.
                requested_turn_mode=requested_turn_mode if agent is not None else "",
                requested_turn_mode_by=(
                    requested_turn_mode_by
                    if agent is not None and requested_turn_mode
                    and getattr(requested_turn_mode_by, "is_authenticated", False)
                    else None),
            )
            _apply_capability(turn, capability)
    except IntegrityError:
        # Only possible race: same idempotency key inserted concurrently.
        replay = Turn.objects.filter(idempotency_key=idempotency_key).first()
        if replay is not None:
            return replay, False
        raise
    if origin == Turn.ORIGIN_EMAIL:
        _log_unproven_member(turn)
    if session is not None:
        # New work on the session: it was not done, so drop any pending "done" push.
        from apps.push import services as push_services

        push_services.cancel_session_finish_push(session.pk)

    # Tell whoever is watching that this ask now exists and where it stands.
    # Post-commit for the same reason `turn_events_appended` is: a subscriber
    # re-reads the turn, and inside the transaction it would read a row nobody
    # else can see yet. Never raises — telling someone about a turn must not be
    # able to undo enqueueing it.
    def _fire_status():
        from .signals import turn_status_changed

        try:
            turn_status_changed.send(sender=Turn, turn=turn)
        except Exception:  # noqa: BLE001 — a status is never worth a failed send
            logger.exception("turn_status_changed receiver failed for %s", turn.pk)

    transaction.on_commit(_fire_status)
    return turn, True


def heartbeat(
    runner: Runner, *, active_turn_ids: list[str], degraded: bool = False, note: str = "",
    ready: bool = True, ready_note: str = "", code_branch: str = "",
    code_version: str = "", code_sha: str = "", code_committed_at: int = 0,
    projects: list[str] | None = None, profiles: int = 0,
    health: dict | None = None, mailboxes_readable: list[str] | None = None,
    envelope: int = 0, midturn: int = 0,
) -> Runner:
    """`profiles` is the profile-enforcement version the runner REPORTS it can
    honour (see `profile_q`). Written on every beat, and 0 from a runner that
    does not send it — so a downgrade takes effect on the next beat, and nothing
    but the runner itself can claim the capability for it.

    `projects` is the runner REPORTING which repos it can drive (spec
    2026-07-28) — the answer emdash already had, replacing a list a human typed
    once at pairing and nothing kept true.

    `None` is not `[]`. None means "I could not tell this tick" and must leave the
    stored list untouched; [] means "I genuinely have none". Collapsing the two
    would let one unreadable emdash DB blank the list and make every repo turn on
    this runner unclaimable — `replace_reported_sessions` learned this with
    sessions, where "swallowing the error is what let a schema drift blank the
    supervisor with nothing in the log".

    Only the one key is written. `sessions` gates chat routing and `agents` is
    still read by older paths; replacing the whole dict would unwire both.

    `mailboxes_readable` follows the same None-is-not-[] rule: None leaves the
    stored list (and its `mailboxes_checked_at`) alone.
    """
    now = timezone.now()
    runner.last_heartbeat_at = now
    runner.status = Runner.DEGRADED if degraded else Runner.ONLINE
    runner.status_note = note
    runner.ready = ready
    runner.ready_note = ready_note
    runner.code_branch = code_branch
    runner.code_version = code_version
    runner.code_sha = code_sha
    runner.code_committed_at = code_committed_at
    fields = [
        "last_heartbeat_at", "status", "status_note", "ready", "ready_note", "code_branch",
        "code_version", "code_sha", "code_committed_at",
    ]
    if projects is not None:
        # Strip blanks even though the runner does too: a session turn has
        # project="", so a stray "" here would match EVERY session turn via
        # `project__in`. Cheap, and the server should not need the report to be
        # well-formed to stay safe.
        cleaned = [p.strip() for p in projects if p and p.strip()]
        if cleaned != runner.capabilities.get("projects"):
            runner.capabilities = {**runner.capabilities, "projects": cleaned}
            fields.append("capabilities")
    if health is not None:
        runner.health = {**health, "received_at": now.isoformat()}
        fields.append("health")
    if mailboxes_readable is not None:
        # Lowercased and de-duplicated here, not trusted from the wire: the
        # doorbell compares against `InboundMailbox.address` case-insensitively.
        runner.mailboxes_readable = sorted(
            {a.strip().lower() for a in mailboxes_readable if a and a.strip()}
        )
        runner.mailboxes_checked_at = now
        fields += ["mailboxes_readable", "mailboxes_checked_at"]
    if int(runner.capabilities.get("profiles") or 0) != int(profiles or 0):
        runner.capabilities = {**runner.capabilities, "profiles": int(profiles or 0)}
        if "capabilities" not in fields:
            fields.append("capabilities")
    # The caller-envelope version the runner's CODE reads (`envelope`), beside the
    # guard version it reports as `profiles`: same contract — written every beat,
    # absent = 0, and part of `profile_q`.
    if int(runner.capabilities.get("envelope") or 0) != int(envelope or 0):
        runner.capabilities = {**runner.capabilities, "envelope": int(envelope or 0)}
        if "capabilities" not in fields:
            fields.append("capabilities")
    # Whether this runner's CODE delivers a follow-up into a running turn
    # (`rides_turn`, canopy-web#1153). Reported, same contract as `envelope`:
    # an older runner would claim a rider as an ordinary chat turn and bridge
    # the same reply twice, so it must never be handed one.
    if int(runner.capabilities.get("midturn") or 0) != int(midturn or 0):
        runner.capabilities = {**runner.capabilities, "midturn": int(midturn or 0)}
        if "capabilities" not in fields:
            fields.append("capabilities")
    runner.save(update_fields=fields)
    # The update nudge: this is the one moment both shas are in hand — the
    # deploy moved the expectation, the beat just reported what's installed.
    # Ring the box's control channel so its updater checks NOW instead of
    # waiting out its 30-min timer. Push is the doorbell, the timer stays the
    # auditor (and the rescue for a daemon too dead to hear a frame). Sent on
    # every stale beat, not edge-triggered: the frame is tiny, and the runner
    # owns the throttle — a missed frame then costs one beat, not one timer
    # cycle. Empty on either side is UNKNOWN, never "stale".
    expected = runner.expected_code_sha()
    if code_sha and expected and code_sha != expected:
        from apps.realtime import groups

        groups.publish(
            groups.runner_group(runner.pk),
            {"type": "runner.update_available", "expected_sha": expected},
        )
    if active_turn_ids:
        Turn.objects.filter(
            pk__in=active_turn_ids,
            claimed_by=runner,
            status__in=[Turn.CLAIMED, Turn.RUNNING, Turn.NEEDS_HUMAN],
        ).update(lease_expires_at=now + dt.timedelta(seconds=DEFAULT_LEASE_SECONDS))
        # A rider lives exactly as long as the turn it rode into: renewed with
        # it, so it can only be swept when that turn is.
        Turn.objects.filter(
            rides_turn_id__in=active_turn_ids,
            claimed_by=runner,
            status__in=[Turn.CLAIMED, Turn.RUNNING, Turn.NEEDS_HUMAN],
        ).update(lease_expires_at=now + dt.timedelta(seconds=DEFAULT_LEASE_SECONDS))
    # The fleet's heartbeats are canopy's only clock (no celery, no beat), so the
    # delayed "your chat has gone quiet" pushes are drained here. Best-effort: a
    # push must never cost a runner its heartbeat.
    try:
        from apps.push import services as push_services

        push_services.send_due_session_pushes(now)
    except Exception:  # noqa: BLE001
        logger.exception("push: draining due session pushes failed")
    # Same clock: shared chat secrets past their 30 minutes are deleted here, so
    # the ciphertext goes even if nobody reads the chat again.
    try:
        from apps.canopy_sessions import secrets as session_secrets

        session_secrets.purge_expired(now)
    except Exception:  # noqa: BLE001
        logger.exception("secrets: purging expired session secrets failed")
    # Same clock again: content past its retention rule. A no-op unless
    # CANOPY_RETENTION_ENFORCE is on, at most hourly, and never raises.
    from apps.retention import services as retention_services

    retention_services.maybe_sweep(now)
    return runner


def mark_running(turn: Turn, *, session_id: str = "") -> Turn:
    """Transition CLAIMED|RUNNING -> RUNNING. A no-op (no event, no field
    writes) if the turn was swept to a terminal state (e.g. lost) underneath
    the caller — guards against a zombie runner resurrecting a dead turn."""
    now = timezone.now()
    fields: dict = {"status": Turn.RUNNING}
    if not turn.started_at:
        fields["started_at"] = now
    if session_id:
        fields["session_id"] = session_id
    updated = Turn.objects.filter(
        pk=turn.pk, status__in=[Turn.CLAIMED, Turn.RUNNING]
    ).update(**fields)
    turn.refresh_from_db()
    if not updated:
        return turn
    append_events(turn, [{"kind": "status", "payload": {"status": Turn.RUNNING}}])
    return turn


def finish_turn(
    turn: Turn, *, status: str, result_note: str = "", allow_queued: bool = False
) -> Turn:
    """Transition CLAIMED|RUNNING|NEEDS_HUMAN -> DONE|FAILED|MISSED|CANCELLED. A
    no-op (no event, no field writes) if the turn is already terminal —
    idempotent, and guards against resurrecting a turn already swept to lost.

    A QUEUED turn is deliberately NOT finishable by default: a runner must never
    finish a turn it never claimed (the API surfaces that attempt as a 409).
    `allow_queued=True` is the scheduler's opt-in — it is a different actor, and
    a slot nobody ever picked up is the textbook MISSED. Without it, supersede
    would silently skip queued occurrences and the board would accumulate them.
    """
    if status not in (Turn.DONE, Turn.FAILED, Turn.MISSED, Turn.CANCELLED):
        raise ValueError(f"finish status must be done|failed|missed|cancelled, got {status!r}")

    # ---- a follow-up that could not be typed into the running turn waits instead ----
    #
    # Measured live (2026-10-05, the #1153 check): the rider was claimed into the
    # running turn within 5s, then every attempt to type it failed
    # COMPOSER_NOT_VISIBLE and the follow-up ended FAILED — a message that, before
    # mid-turn delivery, would merely have arrived late. A rider's send failure
    # names no session (nothing was typed), so it goes back to the queue marked
    # MIDTURN_FAILED: `may_ride` refuses it, and it is claimed the ordinary way when
    # the running turn ends. Not counted as an attempt — nothing about the request
    # failed, only the shortcut. A rider whose keystrokes DID go out reports its
    # session and stays terminal, so it is never typed twice.
    if status == Turn.FAILED and turn.rides_turn_id and not turn.session_key:
        ref = {**(turn.origin_ref or {}), MIDTURN_FAILED: True}
        waiting = Turn.objects.filter(
            pk=turn.pk, status__in=[Turn.CLAIMED, Turn.RUNNING], session_key=""
        ).update(status=Turn.QUEUED, claimed_by=None, claimed_at=None, lease_expires_at=None,
                 started_at=None, rides_turn=None, origin_ref=ref, result_note=result_note)
        turn.refresh_from_db()
        if waiting:
            append_events(turn, [{"kind": "status", "payload": {
                "status": Turn.QUEUED, "midturn_failed": True, "detail": result_note}}])
            return turn

    # ---- a turn that never got a session is a NON-attempt, not a failed attempt ----
    #
    # `session_key` is written the moment cdp_control creates the session and the
    # runner reports it at finish; every runner failure path (client.fail_turn) sends
    # no task id at all. So an empty value on a FAILED turn is proof that no agent
    # ever received the prompt: nothing was read, nothing was sent, no partial work
    # exists. Re-running it is side-effect-free by construction, which is exactly what
    # makes this safe to do automatically and what makes the opposite case (a session
    # DID exist) something we must never retry blind.
    #
    # WHY THIS EXISTS. Every origin except the scheduler was silently at-most-once. A
    # scheduled slot's identity is the CLOCK -- fire_schedule mints `sched:<id>:<slot>`
    # -- so the next cron tick generates a genuinely new key and the work simply
    # happens again; that is why cron looked reliable and nothing else did. An email
    # turn's identity is `email-<agent>-<thread>-<messageCount>`, i.e. MAILBOX STATE AT
    # ENQUEUE, and enqueue_turn short-circuits on the key with NO status filter, so the
    # key is spent the instant the row is created -- before anyone knows whether the
    # session will even start. A CDP fault therefore buried the request permanently:
    # the thread stayed UNREAD, the poller re-saw it on every later tick, regenerated
    # the identical key, collapsed onto the dead row, and logged it "seen".
    #
    # Measured: 2026-09-03, jjackson emailed eva "Stripe sessions". Its only turn
    # failed at `emdash create failed: cannot connect to emdash CDP on 127.0.0.1:9223`
    # and nothing looked at that thread again; a human noticed a day later. The same
    # hole swallows `api`-origin dispatches, which is how an agent-to-agent fix brief
    # can vanish with no trace but a `failed` row nobody reads.
    #
    # Backoff needs nothing new. readiness.mark_failed already marks the box not-ready
    # for MARKER_TTL_SECONDS and claim_next_turn will not hand a not-ready runner a
    # turn, so a requeued turn waits for a healthy runner rather than hot-looping on
    # the broken one. MAX_SESSIONLESS_RETRIES caps it so a persistently-down emdash
    # ends terminal and visible instead of churning forever.
    # A DRILL is the one turn this must never touch. Everywhere else, session
    # creation is incidental to the work and its failure says nothing about the
    # request; for a drill, session creation IS the work -- "can this runner still
    # start a session?" -- so a sessionless failure is the drill's ANSWER, not an
    # interruption of it. Requeueing one would retry the probe whose failure is the
    # result, and strand its RunnerDrill at OUTCOME_PENDING while it churned. Keyed on
    # the RunnerDrill FK rather than the origin, matching the two hooks below: a drill
    # is an ordinary `api` turn that names its runner.
    if (
        status == Turn.FAILED
        and not turn.session_key
        and turn.attempts < MAX_SESSIONLESS_RETRIES
        and not RunnerDrill.objects.filter(turn=turn).exists()
    ):
        now = timezone.now()
        requeued = Turn.objects.filter(
            pk=turn.pk, status__in=[Turn.CLAIMED, Turn.RUNNING], session_key=""
        ).update(
            status=Turn.QUEUED,
            attempts=F("attempts") + 1,
            claimed_by=None,
            claimed_at=None,
            lease_expires_at=None,
            started_at=None,
            result_note=result_note,
        )
        turn.refresh_from_db()
        if requeued:
            # Its riders were typed into a session it never reached; they do
            # not requeue with it (that would type them a second time).
            _finish_riders(turn, Turn.FAILED)
            append_events(turn, [{"kind": "status", "payload": {
                "status": Turn.QUEUED,
                "requeued_after": Turn.FAILED,
                "attempt": turn.attempts,
                "detail": result_note,
            }}])
            return turn
        # Fell through: the turn was not executing (already terminal, or queued and
        # never claimed). Let the normal path below decide, exactly as before.
    # A turn that RAN TO COMPLETION after a stop was requested. This used to be
    # rewritten DONE -> CANCELLED, on the reasoning that the user asked to stop so a
    # reply that raced the interrupt "is still a cancelled turn". That records the
    # user's INTENT in the field that is supposed to record the OUTCOME, and it costs
    # more than it buys:
    #
    #   * The turn carries a complete reply. Labelling it `cancelled` puts a full
    #     answer under a status that says it was stopped — self-contradictory on its
    #     face, and unreadable to anyone auditing a session later.
    #   * A stop that worked and a stop that was completely ignored both stored
    #     `cancelled`, so the ledger could not tell them apart. That is exactly what
    #     made "stop doesn't seem reliable" so hard to pin down (#649): the record
    #     said the same thing either way.
    #   * A scheduled occurrence that completed did NOT discharge its nag, because
    #     the discharge below keys on DONE and the coercion had moved it. The work
    #     was done and the board still asked for attention.
    #
    # And the "stranded cancel_requested" it was written to prevent is not stranded:
    # the event is on the ledger, which is the durable record, and `cancel_requested`
    # on a DONE turn is precisely the fact worth keeping — we asked, it finished
    # anyway. So keep the outcome truthful and make the failed stop legible instead.
    #
    # Scoped deliberately to this case. `sweep_expired_leases` keeps its own
    # cancel_requested -> CANCELLED rule: there the turn did NOT complete and the
    # runner vanished mid-flight, so nothing knows what the agent did, and neither
    # label is a claim about a finished reply.
    stop_ignored = (
        status == Turn.DONE and turn.events.filter(kind="cancel_requested").exists()
    )
    if stop_ignored:
        result_note = (
            f"{result_note} — NOTE: you asked to stop this turn and it ran to "
            f"completion anyway; the cancel never reached the runner"
        ).lstrip(" —")
    now = timezone.now()
    from_states = [Turn.CLAIMED, Turn.RUNNING, Turn.NEEDS_HUMAN]
    if allow_queued:
        from_states.append(Turn.QUEUED)
    updated = Turn.objects.filter(pk=turn.pk, status__in=from_states).update(
        status=status, finished_at=now, result_note=result_note
    )
    turn.refresh_from_db()
    if not updated:
        return turn
    append_events(turn, [{"kind": "status", "payload": {"status": status, "result_note": result_note}}])
    _finish_riders(turn)
    if stop_ignored:
        # SAY IT WHERE THE PERSON WHO PRESSED STOP IS LOOKING. A `status` event
        # carries no client-visible frame (stream_map.turn_event_to_frames), so the
        # truthful DONE above would otherwise be indistinguishable, on screen, from a
        # turn nobody tried to stop. `error` renders as chat.stream_error.
        append_events(turn, [{"kind": "error", "payload": {
            "detail": "your stop never reached the runner — this turn ran to completion",
        }}])
    if turn.chat_session_id:
        # "Your chat is done" — arms the quiet-timer (or pushes now, if the chat
        # asked for every completion). Never allowed to fail the finish.
        try:
            from apps.push import services as push_services

            push_services.on_session_turn_finished(turn)
        except Exception:  # noqa: BLE001
            logger.exception("push: could not arm the finish push for turn %s", turn.pk)
    # A finished scheduled occurrence discharges any open nag for its schedule —
    # you no longer owe attention to a slot that has since completed.
    if status == Turn.DONE:
        sid = (turn.origin_ref or {}).get("schedule_id")
        if sid:
            resolve_schedule_nags(sid)
    # A drill turn that fails outright (auth expired, environment broken) resolves
    # its RunnerDrill without waiting for the agent's own report callback — the
    # agent may never get far enough to curl the callback at all. Scoped to
    # OUTCOME_PENDING so a drill already resolved by a report is not clobbered.
    # CANCELLED is included too: drills queue behind real executing turns
    # (start_drill's docstring) and the plain /turns/{id}/cancel route has no
    # origin filter, so a queued drill can be cancelled out from under itself —
    # without this it would strand OUTCOME_PENDING forever, the same failure
    # mode this hook exists to prevent for FAILED.
    # Keyed on the RunnerDrill FK, not the origin: a drill is an ordinary `api`
    # turn that names its runner, and the filter no-ops for a non-drill turn.
    #
    # `stop_ignored` is here for the same anti-stranding reason, and it is the one
    # thing that had to move when the DONE -> CANCELLED coercion went away: such a
    # turn USED to arrive here as CANCELLED and resolve its drill. Keying the hook on
    # the status alone would have let it fall through as DONE and strand
    # OUTCOME_PENDING forever — reintroducing, through the side door, the exact
    # failure this hook exists to prevent. The drill's resolution depends on "a stop
    # was requested", not on which label the turn ended up with.
    # A drill turn that finished DONE without the agent ever reporting is a
    # failure too: the report IS the drill's answer, and "the turn ran but the
    # callback never arrived" is exactly the control-plane gap a drill exists to
    # catch. Left alone it stranded OUTCOME_PENDING forever (drill 50 on
    # cloud-ec2-1, 2026-09-22, whose report had 404'd). The agent reports DURING
    # its turn, so by now any report has already landed and the PENDING filter
    # below leaves it untouched.
    unreported = status == Turn.DONE and not stop_ignored
    if status in (Turn.FAILED, Turn.CANCELLED) or stop_ignored or unreported:
        if unreported:
            summary = "drill turn finished without reporting"
            if result_note:
                summary += f" — the agent said: {result_note[:1500]}"
        elif stop_ignored:
            summary = "drill turn completed after a cancel that never landed"
        elif status == Turn.CANCELLED:
            summary = "drill turn cancelled"
        else:
            summary = result_note or "drill turn failed"
        RunnerDrill.objects.filter(
            turn=turn, outcome=RunnerDrill.OUTCOME_PENDING
        ).update(outcome=RunnerDrill.OUTCOME_FAIL, summary=summary, finished_at=now)
    # A turn that has really ENDED failed — not requeued sessionless, not a rider
    # sent back to wait (both returned above) — starts a debugger turn
    # (apps/harness/auto_debug.py: deduplicated by fingerprint, capped fleet-wide,
    # and never for a drill, a human's stop, or a debug turn of its own). Jonathan
    # 2026-10-07, after an ACE Slack turn failed and nothing looked at it.
    # on_turn_failed never raises: a debug turn is never worth a failed finish.
    if status == Turn.FAILED:
        from . import auto_debug

        auto_debug.on_turn_failed(turn)
    # A conversation a HUMAN had with an agent just finished: canopy makes that
    # agent remember it (apps/harness/people_digest.py — debounced per agent and
    # person, never for a digest turn or a turn canopy/another agent started).
    # on_turn_finished never raises.
    if status == Turn.DONE:
        from . import people_digest

        people_digest.on_turn_finished(turn)
    return turn


def _finish_riders(turn: Turn, status: str | None = None) -> None:
    """The follow-ups delivered into `turn` end when it does, the same way.

    They were typed into its session, so they have no life of their own: the
    agent answered them in this turn's reply, or the turn's failure/stop took
    them with it. A direct update rather than `finish_turn`, whose sessionless
    requeue would re-type a message that was already delivered."""
    status = status or turn.status
    now = timezone.now()
    for rider in Turn.objects.filter(rides_turn=turn, status__in=EXECUTING):
        note = f"delivered into the running turn {str(turn.pk)[:8]} — {status}"
        if Turn.objects.filter(pk=rider.pk, status__in=EXECUTING).update(
                status=status, finished_at=now, result_note=note):
            rider.status = status
            append_events(rider, [{"kind": "status", "payload": {
                "status": status, "result_note": note, "rode": str(turn.pk)}}])


def cancel_queued_turn(turn: Turn) -> Turn | None:
    """Best-effort un-queue: mark a still-QUEUED turn CANCELLED. Cancel is
    'un-queue', not 'kill' — a CLAIMED/RUNNING turn is owned by its runner's
    lease and is left alone (returns None). Terminal turns also return None
    (idempotent no-op). The REST cancel view and chat's `chat.stop` both route
    through here."""
    if turn.status != Turn.QUEUED:
        return None
    return finish_turn(turn, status=Turn.CANCELLED, result_note="cancelled", allow_queued=True)


def cancel_turn(turn: Turn, *, by: str = "") -> Turn | None:
    """Full cancel semantics for chat.stop / the REST stop route. A QUEUED turn
    is finished CANCELLED immediately. An executing turn is NOT force-finished —
    the runner owns its lease — instead we record cancel_requested in the ledger
    and signal the claiming runner over its control channel; the runner interrupts
    the emdash session and finishes the turn as cancelled (or, if the runner is
    gone, the lease sweep sees cancel_requested and closes it CANCELLED).

    `by` names the person who pressed stop; it rides the ledger (the status event
    for a queued turn, `cancel_requested` for an executing one) so a channel can
    say "Stopped by <name>" — see `stopped_by`."""
    who = {"by": by} if by else {}
    if turn.status == Turn.QUEUED:
        # Race guard (finding M1): this is a read-then-act on `turn.status`, and
        # a runner's claim can land between the read and the write, moving the
        # turn QUEUED -> CLAIMED underneath us. Routing that through
        # finish_turn(allow_queued=True) would be wrong here — its from_states
        # already includes CLAIMED/RUNNING/NEEDS_HUMAN, so it would happily
        # force-finish the now-claimed turn as CANCELLED out from under the
        # runner that just picked it up. Guard the UPDATE itself on
        # status=QUEUED so only a turn still queued at write-time is affected.
        now = timezone.now()
        updated = Turn.objects.filter(pk=turn.pk, status=Turn.QUEUED).update(
            status=Turn.CANCELLED, finished_at=now, result_note="cancelled",
        )
        turn.refresh_from_db()
        if updated:
            append_events(turn, [{
                "kind": "status",
                "payload": {"status": Turn.CANCELLED, "result_note": "cancelled", **who},
            }])
            # Mirror finish_turn's drill hook (bypassed above) so a queued
            # drill cancelled this way doesn't strand OUTCOME_PENDING. Keyed on
            # the RunnerDrill FK — no-ops for a non-drill turn.
            RunnerDrill.objects.filter(
                turn=turn, outcome=RunnerDrill.OUTCOME_PENDING
            ).update(outcome=RunnerDrill.OUTCOME_FAIL, summary="drill turn cancelled", finished_at=now)
            return turn
        # Lost the race: the turn is no longer QUEUED (claimed out from under
        # us, or already terminal). Fall through to the freshly-refreshed
        # status below rather than the stale one we started with.
    if turn.status in (Turn.CLAIMED, Turn.RUNNING, Turn.NEEDS_HUMAN) and turn.rides_turn_id:
        # A rider is a message inside the running turn's session, so stopping it
        # IS stopping that turn — the runner interrupts the session, and the
        # rider ends with it (`_finish_riders`).
        holder = turn.rides_turn
        if holder is not None and holder.status in (Turn.CLAIMED, Turn.RUNNING, Turn.NEEDS_HUMAN):
            cancel_turn(holder, by=by)
            return turn
    if turn.status in (Turn.CLAIMED, Turn.RUNNING, Turn.NEEDS_HUMAN):
        append_events(turn, [{"kind": "cancel_requested", "payload": who}])
        if turn.claimed_by_id:
            from apps.realtime import groups

            groups.publish(groups.runner_group(turn.claimed_by_id),
                           {"type": "runner.cancel", "turn_id": str(turn.id)})
        return turn
    return None


def stopped_by(turn: Turn) -> str:
    """Who pressed stop on this turn, or "" (nobody recorded, or not a person's stop)."""
    for ev in turn.events.filter(kind__in=["cancel_requested", "status"]).order_by("-seq"):
        by = (ev.payload or {}).get("by")
        if by:
            return str(by)
    return ""


# --------------------------------------------------------------------------------------
# RunnerBinding reuse — durable thread↔session mapping (cross-account); see
# apps.canopy_sessions.models.RunnerBinding
# --------------------------------------------------------------------------------------

def _binding_for_thread(agent, project, workspace, thread_key):
    """The RunnerBinding for a (target, thread_key), or None. Enforces the
    agent-XOR-project rule the way _link_target used to: an agent thread matches on
    session.agent and ignores workspace (derived via the agent); a project thread
    matches on session.project AND session.workspace (its identity, so a guessed
    thread_key from another tenant lands on its own row, never the victim's)."""
    from apps.canopy_sessions.models import RunnerBinding

    if bool(agent) == bool(project):
        raise ValueError("a session reuse lookup targets an agent XOR a project")
    qs = RunnerBinding.objects.select_related("session", "runner").filter(thread_key=thread_key)
    if agent:
        return qs.filter(session__agent=agent).first()
    if workspace is None:
        raise ValueError("a project session reuse lookup needs a workspace")
    return qs.filter(
        session__agent__isnull=True, session__project=project, session__workspace=workspace
    ).first()


class ThreadSessionNotFound(LookupError):
    """A thread_key named an existing Session that is not this thread's to bind.
    The API answers 404 — the same as a session that does not exist — so a
    runner cannot learn which session ids are real."""


def _session_is_this_threads(session, agent, project, workspace, runner) -> bool:
    """May `runner` bind the EXISTING `session` as the thread (agent | project)?

    Binding re-points the session's live hint, its streams and its next turns at
    this box, so a session id must not be a key to someone else's conversation:
    it has to be a session OF this target — the same agent, or the same repo in
    the same workspace — in a workspace the runner's owner belongs to. Fails
    closed on a runner with no owner (no pairing human, no tenant)."""
    if agent is not None:
        if session.agent_id != agent.pk:
            return False
    elif (session.agent_id is not None or session.project != (project or "")
          or workspace is None or session.workspace_id != workspace.pk):
        return False
    owner = getattr(runner, "owner", None)
    return owner is not None and wsvc.member_role(owner, session.workspace_id) is not None


def _thread_session(agent, project, workspace, thread_key, *, runner):
    """Find-or-create the durable Session a thread maps to. A chat thread_key is
    str(session.id) — bind that exact existing Session, when it is this thread's
    (`_session_is_this_threads`; else ThreadSessionNotFound). Otherwise create a
    durable origin=runner Session for the phone/agent/project thread.

    The Session's workspace is the one the caller pinned, else the agent's own
    home (Agent.workspace is NOT NULL). A thread with neither has no tenant, and
    guessing one would file it in a workspace nobody chose — so it fails loudly."""
    from apps.canopy_sessions.models import Session

    try:
        existing = Session.objects.filter(pk=uuid.UUID(str(thread_key))).first()
    except (ValueError, TypeError):
        existing = None
    if existing is not None:
        if not _session_is_this_threads(existing, agent, project, workspace, runner):
            raise ThreadSessionNotFound(thread_key)
        return existing
    home = workspace or (agent.workspace if agent else None)
    if home is None:
        raise ValueError("a new thread session needs an agent or a workspace")
    return Session.objects.create(
        agent=agent,
        project=project or "",
        workspace=home,
        origin=Session.ORIGIN_RUNNER,
        title=thread_key[:200],
    )


#: How an email thread names its Session. Namespaced like `emdash:<task>` so the
#: three producers of a thread_key cannot collide.
EMAIL_THREAD_PREFIX = "email:"

#: Where the Gmail thread id is remembered on the Session, so the SECOND message
#: on a thread finds the FIRST message's session instead of making a new one.
#: `_thread_session` cannot serve this: it looks a thread_key up only as a
#: Session UUID, so any non-UUID key creates a fresh row every single time.
EMAIL_THREAD_KEY = "email_thread_key"


def email_thread_session(agent, thread_id: str, subject: str = ""):
    """The durable Session for one Gmail thread — found, or created once.

    An inbound email already becomes a Turn. A Turn is a fine unit of execution
    and a poor unit of CONVERSATION: it ends, and there is nothing left for a
    human to open, watch, or type into. A Session is the thing ace-web already
    renders through the shared chat kit, so binding the two is what turns "ACE
    replied to that email eventually" into "here is the run, live, and you can
    steer it".

    Keyed on the Gmail thread, which gives the behaviour a reader expects for
    free: a reply on the same thread continues the same session; a new thread
    opens a parallel one.

    `origin_key` is stamped because that is the field ace-web's session list
    filters on — a session without it exists and is invisible, which is the
    least useful possible outcome.
    """
    from apps.canopy_sessions.models import Session

    if agent is None:
        raise ValueError("an email thread session belongs to an agent")
    key = f"{EMAIL_THREAD_PREFIX}{thread_id}"
    # A KEY-PATH lookup, not `metadata__contains`: `contains` on a JSONField is
    # PostgreSQL-only and raises NotSupportedError on SQLite, so the production
    # path would have worked while every test errored — found by running them.
    existing = (
        Session.objects.filter(agent=agent, **{f"metadata__{EMAIL_THREAD_KEY}": key})
        .order_by("created_at")
        .first()
    )
    if existing is not None:
        return existing
    workspace = agent.workspace  # NOT NULL: an agent always has its one home
    return Session.objects.create(
        agent=agent,
        workspace=workspace,
        origin=Session.ORIGIN_RUNNER,
        title=(subject or key)[:200],
        metadata={
            EMAIL_THREAD_KEY: key,
            # ace-web lists sessions by this; see its CLAUDE.md § canopy-hosted chat.
            "origin_key": f"ace-web:{workspace.slug}",
            "source": "email",
        },
    )


def _busy_reason(binding, turn_id) -> str:
    """Why the session `binding` points at must not take a prompt right now, or "".

    A prompt is only ever sent into an IDLE session (#309): typing into one that is
    mid-turn lands in the live turn's conversation and corrupts it. Two observations
    say busy, and neither is inferred from write recency (which would also fire for a
    session whose last turn ended a minute ago, forking every quick follow-up):

    - another EXECUTING turn holds it: a chat turn on the same Session, or an
      agent/project turn whose recorded session is this one on this box. Agent turns
      already serialize per agent (`one_executing_turn_per_agent`); project turns do
      not, and a chat turn is serialized per SESSION, not per agent.
    - the engine says it is working (`agent_status`, emdash's own flag, or the
      runner's `agent_status_stale` dissent): a person typing in it, or work that
      outlived its turn. A blank status (the runner could not answer) is not busy."""
    from apps.canopy_sessions import services as session_services

    others = Turn.objects.filter(status__in=EXECUTING).exclude(pk=turn_id)
    holder_q = Q(chat_session_id=binding.session_id)
    if binding.session_key and binding.runner_id:
        holder_q |= Q(session_key=binding.session_key, claimed_by_id=binding.runner_id)
    holder = others.filter(holder_q).values_list("pk", flat=True).first()
    if holder is not None:
        return f"turn {holder} is executing in it"
    if binding.agent_status and session_services.is_session_running(binding):
        return f"the engine reports it {binding.agent_status}"
    return ""


def resolve_session(agent, thread_key: str, runner: Runner, *, project: str = "", workspace=None,
                    turn_id=None) -> dict:
    """Given (target, thread_key) and the CURRENTLY-active runner, decide how to execute.

    `agent` may be None when `project` is given — the phone addresses repos too.

    Returns a plan dict:
      - reuse (bool): the live session hint is owned by THIS runner/host — the runner
        should verify the emdash task still exists and drive it (send prompt into it).
      - session_key: the session to reuse (only meaningful when reuse=True);
        `emdash_task_id` carries the same value for runners predating the rename.
      - agent_task_ext_id / summary: durable context for rehydration when reuse=False
        (fresh session under this account) or for a brand-new thread.
      - link_id: the RunnerBinding's session id (None if no binding exists yet — brand-new
        thread).

    Never assumes the live session is reachable: reuse is only proposed when the hint's
    runner + macOS host match the caller (the two-account failover invariant).

    `turn_id` names the agent/project turn asking. With it, reuse is also refused
    when the session is mid-turn (`_busy_reason`, #309) and `busy` says why; the
    runner then creates a fresh session, as it does for one that is gone. A CHAT
    turn is exempt: its session IS the conversation, which claim already
    serializes and a rider (#1153) joins on purpose. Without `turn_id` (a runner
    predating it) the answer is unchanged."""
    binding = _binding_for_thread(agent, project, workspace, thread_key)
    if binding is None:
        return {"reuse": False, "session_key": "", "emdash_task_id": "", "agent_task_ext_id": "",
                "summary": "", "link_id": None, "new_thread": True, "busy": ""}
    reuse = binding.reusable_by(runner)
    busy = ""
    if reuse and turn_id is not None:
        # Only a chat turn THIS runner holds is exempt; any other id is guarded.
        turn = Turn.objects.filter(pk=turn_id, claimed_by=runner).only("chat_session_id").first()
        if turn is None or turn.chat_session_id is None:
            busy = _busy_reason(binding, turn_id)
            reuse = not busy
    return {
        "reuse": reuse,
        "busy": busy,
        "session_key": binding.session_key,
        "emdash_task_id": binding.session_key,
        "agent_task_ext_id": binding.agent_task_ext_id,
        "summary": binding.summary,
        "link_id": str(binding.session_id),
        "new_thread": False,
    }


def _aware(value: dt.datetime | None) -> dt.datetime | None:
    """Coerce a possibly-naive datetime to aware (UTC). The runner sends ISO8601
    (typically already UTC via a trailing "Z"), but a naive value would otherwise
    hit Django's USE_TZ=True as a silent local-time footgun rather than a clean
    UTC stamp."""
    if value is None:
        return None
    if timezone.is_naive(value):
        return timezone.make_aware(value, dt.UTC)
    return value


_COMMAND = re.compile(r"^/[\w:.-]+")          # "/ace:turn", "/eva:turn"
_FLAG = re.compile(r"^--?[\w-]+(?:[ =](?!--)\S+)?")  # "--thread 1a0f…", "--x=y"
TITLE_CAP = 80


def readable_title(text: str) -> str:
    """A name a person can read, from a prompt's first line — or "" when the line
    is nothing but a command. A cloud turn's prompt is often a slash command
    ("/ace:turn --thread 1a0f24bf9b830273"), which made a session's title the
    command itself (2026-10-03); the words after it, if any, are the name."""
    line = next((ln.strip() for ln in (text or "").splitlines() if ln.strip()), "")
    if line.startswith("/"):
        rest = _COMMAND.sub("", line, count=1).strip()
        while True:
            m = _FLAG.match(rest)
            if not m:
                break
            rest = rest[m.end():].strip()
        line = rest.lstrip("—–-:· ").strip()
    if not line:
        return ""
    # One sentence, cut at a word: a title, not the brief.
    first = re.split(r"(?<=[.!?])\s", line, maxsplit=1)[0].rstrip(".")
    if len(first) > TITLE_CAP:
        first = first[:TITLE_CAP].rsplit(" ", 1)[0].rstrip(",;:") + "…"
    return first[:1].upper() + first[1:]


def turn_session_title(turn, proposed: str = "") -> str:
    """What a session started by `turn` is called: the email's subject, else its
    schedule's name, else a readable form of what the runner proposed (or of the
    prompt), else "<Agent> turn". Never a bare command."""
    ref = turn.origin_ref or {}
    for key in ("subject", "schedule_name"):
        value = ref.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()[:200]
    named = readable_title(proposed) or readable_title(turn.prompt or "")
    if named:
        return named[:200]
    agent = turn.agent or (turn.chat_session.agent if turn.chat_session_id else None)
    return f"{agent.name} turn" if agent else "Turn"


def record_session(
    agent,
    thread_key: str,
    *,
    runner: Runner,
    project: str = "",
    workspace=None,
    session_key: str = "",
    session_id: str = "",  # accepted for wire-compat; the binding keys on session_key
    agent_task_ext_id: str | None = None,
    summary: str | None = None,
    title: str = "",
):
    """Upsert the thread's durable Session + RunnerBinding and re-point the live-session
    hint at THIS runner/host. Only overwrites agent_task_ext_id/summary when passed,
    preserving accumulated context. The API caller has already gated the runner's
    owner against `workspace` — this stores, it does not authorize."""
    from apps.canopy_sessions.models import RunnerBinding

    with transaction.atomic():
        binding = _binding_for_thread(agent, project, workspace, thread_key)
        if binding is None:
            session = _thread_session(agent, project, workspace, thread_key, runner=runner)
            binding = (
                RunnerBinding.objects.select_for_update()
                .filter(session=session)
                .first()
            )
            if binding is None:
                binding = RunnerBinding(session=session)
        binding.thread_key = thread_key
        binding.runner = runner
        binding.host = runner.host
        binding.session_key = session_key
        # The other half of the runner-side key (see RunnerBinding.emdash_project).
        # Mirrors what `Session.emdash_project` derives, from the arguments this
        # path already has: a project thread carries its repo, an agent thread its
        # agent's slug — which is the repo emdash actually runs it under.
        binding.emdash_project = project or (agent.slug if agent else "")
        # Name the row after the emdash task the human actually sees.
        # _thread_session titles a BRAND-NEW session with the raw thread_key,
        # which for an agent turn is an opaque hash (a real one leaked into the
        # Sessions list as "19f91250349ec91b"). Only retitle when the title is
        # still that fallback — never clobber a human-set chat title.
        # Retitle when the current title is NOT a name the human chose. Two
        # cases qualify: the raw thread_key fallback (an opaque hash for an agent
        # turn — one leaked into the Sessions list as "19f91250349ec91b"), and an
        # AUTOTITLE, which is the first user message truncated to 80 chars.
        #
        # The autotitle case is why this is more than an equality check: a
        # phone-created session gets autotitled before any emdash task exists, so
        # the old `title == thread_key` guard never matched and the session kept a
        # sentence for a name while the sidebar showed the task
        # (observed 2026-07-27). A human-set title is still never clobbered — it
        # won't match the fallback and won't match the first message either.
        #
        # A runner-supplied `title` beats the key: a cloud runner's key is a
        # Claude session UUID, which names nothing a person would recognise.
        name = (title or "").strip() or session_key
        if name and _title_is_derived(binding.session, thread_key):
            binding.session.title = name[:200]
            binding.session.save(update_fields=["title"])
        binding.live_seen_at = timezone.now()
        if agent_task_ext_id is not None:
            binding.agent_task_ext_id = agent_task_ext_id
        if summary is not None:
            binding.summary = summary
        binding.save()
        # A widget can attach before this binding exists — see seed_stream_desired.
        from apps.canopy_sessions.services import seed_stream_desired

        seed_stream_desired(binding)
    return binding


def stamp_turn_session(turn_id, runner: Runner, session_key: str) -> bool:
    """Give a running turn its session key the moment its session exists.

    `finish` writes `session_key` too, but finish comes LAST — after the
    agent has already posted its close-out report, which joins on that key
    (apps/agents/services._claim_dispatch_row). A laptop finishes seconds in
    (emdash runs the work after), so it never noticed; a cloud runner finishes
    when the work does, so every cloud close-out found no turn and became a
    second, report-only row (labs, echo, 2026-10-02 17:00).

    Only the runner that claimed the turn may stamp it, only while it runs, and
    only an empty key — the same first-write-wins rule `finish` keeps, so the
    two can never disagree. True if a row was stamped."""
    return bool(
        Turn.objects.filter(
            pk=turn_id, claimed_by=runner, session_key="",
            status__in=[Turn.CLAIMED, Turn.RUNNING, Turn.NEEDS_HUMAN],
        ).update(session_key=session_key[:200])
    )


@transaction.atomic

def _title_is_derived(session, thread_key: str) -> bool:
    """True when a session's title was generated rather than chosen by a human.

    Generated titles are safe to replace with the emdash task name; a title
    somebody typed is not. Two generators exist: `_thread_session`'s raw
    thread_key fallback, and `autotitle.maybe_autotitle`, which takes the first
    user message, collapses whitespace and truncates to TITLE_MAX.
    """
    from apps.canopy_sessions.autotitle import TITLE_MAX
    from apps.canopy_sessions.models import Message

    title = (session.title or "").strip()
    if not title or title == thread_key:
        return True
    first = (
        Message.objects.filter(session=session, role=Message.USER)
        .order_by("turn_index")
        .values_list("plaintext", flat=True)
        .first()
    )
    if not first:
        return False
    return title == " ".join(first.split())[:TITLE_MAX]


def _reported_project(s) -> str:
    """The emdash PROJECT of a reported session, normalised to a string.

    getattr rather than attribute access for the same reason `agent_status` and
    `question` use it below: lightweight session objects also reach this service,
    and half of an identity must never be the reason a liveness report 500s.

    This is the value `Session.emdash_project` answers with on the way back out
    (`get_session_streams` ships it next to `session_key`), so a laptop-reported
    agent chat lines up: emdash runs it under the agent's own repo, which is the
    same string `emdash_project` derives from `agent.slug`.
    """
    return getattr(s, "project", "") or ""


def _agent_for_project(project: str):
    """The agent a reported emdash PROJECT belongs to, or None.

    The wholesale sweep (`POST /runners/{id}/sessions`) reports every open task a
    runner can see and carries NO agent, so canopy had nothing to attribute the
    work with and fell back to the RUNNER's workspace. In practice every runner is
    registered in `dimagi` while ace/ada/echo/hal all live in `connect`, so every
    one of their sessions was filed under a tenant that does not contain the agent
    it is about — readable by that tenant's members, and invisible to any lister
    scoped to the agent's own. The feed also reported `agent: null` for all of
    them, which is the same defect seen from the other side.

    `project` IS the agent's repo by convention — the mapping
    `Session.emdash_project` already states in reverse ("an agent chat leaves
    project blank, but its worktree is still under the agent's own repo"). So a
    project naming an agent is that agent's work, and the tenant follows from the
    agent rather than from whichever machine happened to run it.

    Returns None for a real repo checkout that is nobody's agent (canopy-web,
    connect-labs). Those keep the runner's workspace, which is right for them.
    """
    from apps.agents.models import Agent

    if not project:
        return None
    return Agent.objects.select_related("workspace").filter(slug=project).first()


def fire_sessions_closed(session_ids: list) -> None:
    from apps.canopy_sessions.models import Session
    from apps.harness.signals import sessions_closed

    try:
        sessions_closed.send(sender=Session, session_ids=list(session_ids))
    except Exception:  # noqa: BLE001 — a notifier must never fail the report
        logger.exception("sessions_closed receivers failed")


# What a dialog IS, as opposed to how it was last seen: its question and the
# options offered. `observed_at` is re-stamped on every read, `selected` follows
# the cursor, `answer_*` report on a tap, and `title`/`body`/`source` differ
# between the hook, transcript and screen producers for the SAME dialog — none
# of those make it a different question. Mirrors `apps.slack.menus.content_key`,
# which dedupes the Slack post the same way.
def _ask_identity(menu):
    if not menu:
        return None
    return (str(menu.get("question") or ""),
            tuple(str(o.get("label") or "") for o in (menu.get("options") or [])
                  if isinstance(o, dict)))


def replace_reported_sessions(
    runner: Runner, workspace, sessions: list, archived: list[str] | None = None,
    complete: bool = False,
) -> int:
    """Upsert a durable Session(origin=runner) + RunnerBinding per reported
    session. Sessions that fell off the report keep their Session row but have
    their live binding cleared.

    `archived` is the CLOSING signal — emdash task names this runner has seen
    archived. Absence from `sessions` is ambiguous (archived? runner down?
    truncated?), so it can never retire a row on its own; an explicit name here can.
    Scoped to THIS runner's bindings, because a task name is not unique across
    machines and one laptop must never retire another's session.

    The SAME binding this writes doubles as the reuse target for a phone-
    dispatched "Continue" turn (`origin_ref.thread_key = "emdash:<task>"`,
    e.g. `OpenSessions.tsx`) — `thread_key` + `host` are stamped ONLY when the
    binding is freshly created here, so `_binding_for_thread` can find a
    project-Continue row that has no other origin. Pre-fold (SessionLink era)
    a SECOND row existed purely for that lookup; now there is only one row,
    but an existing binding's durable identity (thread_key/host) is left
    untouched on update — the runner's ambient sweep reports EVERY open
    emdash task (agent- or project-driven, no filter), so a session already
    bound by `record_session` to an agent/phone thread must not have that
    binding's thread_key silently reassigned to `emdash:<task>` underneath it
    (that would orphan the agent thread's reuse lookup and fork a duplicate
    session on its next turn)."""
    from apps.canopy_sessions.models import RunnerBinding, Session

    # Record that this runner PARTICIPATES in the wholesale report — the observer
    # session staleness is derived against (see Runner.sessions_reported_at and
    # canopy_sessions.staleness.unseen_q). Stamped BEFORE the early-outs and
    # regardless of whether `sessions` is empty: an empty report is the case that
    # must retire everything on the box, so it is exactly the one that has to count
    # as having reported. A targeted one-column `update()`, never `runner.save()` —
    # this service is called on a ~10s cadence and a full save here would join the
    # bug class where a frequent writer silently clears fields another writer owns
    # (`code_sha`/`code_branch` have each paid for that once).
    Runner.objects.filter(pk=runner.pk).update(sessions_reported_at=timezone.now())

    # emdash task NAMES are not unique — two un-archived tasks can share a name
    # (see task_state's "Names aren't unique in emdash's schema" note). Collapse
    # duplicates before upserting; the runner sends newest-first, so the first
    # occurrence is the live session and an older namesake is stale and correctly
    # dropped (observed 2026-07-20 with two "mobile" tasks).
    #
    # Keyed on (PROJECT, name), not the bare name. A name is only reused-and-stale
    # within ONE project; the same name in two projects is two live conversations,
    # and collapsing those dropped the second from the report ENTIRELY — no row, no
    # error, invisible on the web. That is the more expensive half of the collision
    # this key fixes, because a mislabelled session is at least visible.
    deduped, seen = [], set()
    for s in sessions:
        ident = (_reported_project(s), s.emdash_task)
        if ident in seen:
            continue
        seen.add(ident)
        deduped.append(s)

    now_keys = {s.emdash_task for s in deduped}
    # The bindings this report actually touched. Identity by ROW, not by name: the
    # reconciliation below (un-archive, stale menus, unsatisfied closes) has to say
    # "this exact session was in the report", and `session_key__in=now_keys` cannot
    # — it would revive a project's archived namesake because a DIFFERENT project's
    # task of the same name is alive.
    touched_ids: list = []
    # (session_id, menu|None) for every session whose dialog appeared or went
    # away in THIS report — pushed after commit so an open chat updates without
    # a reload. Only the edges: the report repeats every ~10s and republishing
    # an unchanged menu would re-render the buttons under a thumb.
    menu_changes: list[tuple] = []
    new_asks: list[tuple] = []

    # The loop takes select_for_update locks, which Django REJECTS outside a
    # transaction — "select_for_update cannot be used outside of a transaction".
    # That was latent from #350: nothing in the loop wrote after taking the lock,
    # so the error was never raised. Adding a `.save()` (the title repair) made it
    # fire, 500ing the endpoint the runner calls every ~10s to report which
    # sessions are alive — the whole machine went dark on liveness.
    #
    # Wrapping the loop is the real fix: the lock was always meant to serialize
    # concurrent reports for a task, and without a transaction it never did.
    with transaction.atomic():
        # Every candidate binding for this report, locked and loaded in ONE query
        # with its session, instead of up to two locked lookups plus a lazy
        # session load per reported task — that loop was ~8 queries a session,
        # ~160 a report, from every box every ~10s, all on the one process that
        # also serves every page (SLOW_REQUEST, 2026-10-03). Same predicate as the
        # per-task lookup below, keyed by (task, project) for the same reason.
        candidates: dict[tuple[str, str], RunnerBinding] = {}
        if deduped:
            host_match_all = Q(runner__isnull=True) & Q(host=runner.host) & ~Q(host="")
            for row in (RunnerBinding.objects.select_for_update(of=("self",))
                        .select_related("session")
                        .filter(session_key__in=[x.emdash_task for x in deduped])
                        .filter(Q(runner=runner) | host_match_all)
                        .order_by("pk")):
                candidates.setdefault((row.session_key, row.emdash_project), row)
        for s in deduped:
            # Find this runner's binding for the task WITHOUT depending on the live
            # `runner` FK — the clear step below nulls it for anything that fell off the
            # report, and a lookup keyed on it would then miss the row and fork a
            # DUPLICATE Session when the task reappears. The two branches are
            # asymmetric on purpose: `runner=runner` preserves today's behaviour
            # exactly while the FK is set (legacy bindings carry host="" and would stop
            # matching if we keyed on host alone), and the null branch recovers a row
            # THIS runner previously released — scoped by host, because emdash task
            # names collide across machines and one laptop must never claim another's.
            # The null branch requires a NON-BLANK host: a legacy binding with host=""
            # would otherwise be recoverable by any runner whose own host is "" (two
            # un-heartbeated runners would fuse). `runner=runner` still covers a
            # host="" binding this runner currently owns, so that case is unaffected.
            project = _reported_project(s)
            # The project is HALF THE KEY, not a detail carried alongside it (see
            # RunnerBinding.emdash_project). Matching on the name alone fused two
            # repos' same-named tasks into one row.
            #
            # The second lookup is the legacy branch, and it is what makes this
            # deploy a no-op for every row that already exists: a binding written
            # before 0021 backfilled the column — or by a runner that reports no
            # project at all — carries a blank, and must still be recognised and
            # then FILLED, exactly as `host` and `thread_key` are below. It cannot
            # re-open the hole it closes: a blank is adopted once and is no longer
            # blank, and a binding carrying a DIFFERENT project is never matched.
            binding = candidates.get((s.emdash_task, project))
            if binding is None and project:
                binding = candidates.pop((s.emdash_task, ""), None)
            if binding is None:
                # Tenant AND agent follow the project when it names an agent —
                # not the runner. A runner is a machine serving several agents
                # across tenants, so "whose work is this" is a question its own
                # workspace cannot answer.
                owner = _agent_for_project(project)
                session = Session.objects.create(
                    agent=owner,
                    # A session targets an agent XOR a project
                    # (chat_session_not_agent_and_project), so the project is
                    # cleared when an agent owns it. Nothing is lost:
                    # `Session.emdash_project` returns the agent's slug in that
                    # case, so the (project, task) pair the runner resolves a
                    # transcript by is byte-identical either way — which is also
                    # why RunnerBinding.emdash_project, the key the 10s report
                    # loop reuses on, does not move.
                    project=("" if owner else project),
                    workspace=(owner.workspace if owner else workspace),
                    origin=Session.ORIGIN_RUNNER,
                    title=s.emdash_task,
                )
                binding = RunnerBinding(session=session, session_key=s.emdash_task)
                binding.emdash_project = project
                binding.thread_key = f"emdash:{s.emdash_task}"
                binding.host = runner.host
            else:
                # Correct a GENERATED title on an existing session. A brand-new
                # session above is titled from the emdash task, but an existing one
                # never was — so a session created on the phone kept its autotitle
                # (the first user message, truncated) forever, while emdash's own
                # sidebar showed the task name. The first fix went into
                # `record_session`, which only runs when a TURN is routed; this is
                # the path that runs every ~10s, which is why the repair never
                # actually happened (observed 2026-07-27, after shipping it).
                #
                # `_title_is_derived` recognises only titles WE generated, so a title
                # a human chose is still never touched.
                # Cosmetic repair, kept isolated: a title must never cost liveness.
                # The enclosing `transaction.atomic()` (see below) is what makes the
                # select_for_update above legal at all; this inner block only stops a
                # retitle failure from rolling the whole report back.
                # The cheap comparison first: in the steady state the title already
                # IS the task name, and `_title_is_derived` reads a message to decide.
                try:
                    if binding.session.title != s.emdash_task[:200]:
                        with transaction.atomic():
                            if _title_is_derived(binding.session, binding.thread_key or ""):
                                binding.session.title = s.emdash_task[:200]
                                binding.session.save(update_fields=["title"])
                except Exception:  # noqa: BLE001 — a title must never cost liveness
                    logger.warning("could not retitle session %s from task %r",
                                   binding.session_id, s.emdash_task, exc_info=True)
                # thread_key/host are the binding's durable IDENTITY. NEVER overwrite a
                # non-empty one — an existing binding may be owned by an agent/phone
                # thread (record_session) and this report loop must not steal it (see
                # the docstring above). But DO fill an EMPTY one: bindings predating the
                # SessionLink fold have host="" and can never satisfy
                # RunnerBinding.reusable_by (which requires runner AND host), so a chat
                # sent to one spawned a fresh emdash session forever instead of reusing
                # the live one. Fill-if-empty heals those without clobbering anything.
                if not binding.thread_key:
                    binding.thread_key = f"emdash:{s.emdash_task}"
                if not binding.host:
                    binding.host = runner.host
                # Fill the legacy blank matched above, so the row is keyed properly
                # from here on. Same fill-if-empty shape, and same reason.
                if not binding.emdash_project:
                    binding.emdash_project = project
                # And repair the LABEL on the session itself. `project` was only
                # ever written at creation, so a row that got the wrong one — from
                # the name-only fusion this change removes, or from a blank adopted
                # just above — displayed it forever with nothing to correct it.
                # Guarded on `agent`: an agent chat identifies by its agent and
                # carries a deliberately blank `project` (the model's own
                # chat_session_not_agent_and_project constraint forbids both), so
                # writing one here would violate it.
                #
                # Cosmetic, so isolated like the retitle above: a label must never
                # cost liveness.
                try:
                    sess = binding.session
                    if project and sess.agent_id is None and sess.project != project:
                        with transaction.atomic():
                            sess.project = project
                            sess.save(update_fields=["project"])
                except Exception:  # noqa: BLE001 — a label must never cost liveness
                    logger.warning("could not repair project on session %s to %r",
                                   binding.session_id, project, exc_info=True)
            binding.runner = runner
            binding.status = s.status or ""
            # The engine's own liveness flag, written through EVERY report including
            # blank — the same reasoning as `pending_question` below: this is a fresh
            # observation, so "it stopped working" has to be able to clear "working",
            # or a finished session keeps a live badge forever. getattr for the same
            # reason too: lightweight session objects also reach this service, and a
            # display flag must never be why a liveness report fails.
            binding.agent_status = getattr(s, "agent_status", "") or ""
            # Written through on EVERY report including False, for the same reason
            # as the flag it qualifies: it is a fresh observation, so "it has gone
            # quiet" has to be able to clear "it is still writing".
            binding.agent_status_stale = bool(getattr(s, "agent_status_stale", False))
            binding.last_interacted_at = _aware(s.last_interacted_at)
            binding.live_seen_at = timezone.now()
            # The one write site. See RunnerBinding.reported_at — `close` branches
            # on this, and it is only trustworthy because record_session leaves it
            # alone.
            binding.reported_at = binding.live_seen_at
            binding.tail = list(s.recent_messages or [])
            # Written unconditionally, INCLUDING None. The report is a fresh
            # observation of the session's screen every ~10s, so "no dialog" has
            # to be able to clear one — otherwise a menu answered at the laptop
            # keeps live buttons on every phone that opens the session, and a
            # tap then presses a number at a prompt that is no longer a dialog.
            was_asking = binding.pending_question or None
            # getattr, not attribute access: this service is also called
            # directly with lightweight session objects, and a dialog must
            # never be the reason a liveness report fails.
            binding.pending_question = getattr(s, "question", None) or None
            if was_asking != binding.pending_question:
                menu_changes.append((binding.session_id, binding.pending_question))
                # Whether this is a NEW ask, not the same one seen again. Every
                # producer re-stamps `observed_at` on each read, so the raw dicts
                # differ on every ~10s report for as long as a dialog is up —
                # comparing them is what buzzed a phone every ten seconds
                # (2026-09-22). The frame above still goes out (it carries the
                # fresh stamp); the push is for the ask itself.
                if binding.pending_question and (
                        _ask_identity(was_asking) != _ask_identity(binding.pending_question)):
                    new_asks.append((binding.session_id, binding.pending_question))
            binding.save()
            # The same race as record_session: a viewer may have attached before
            # this report created the binding.
            from apps.canopy_sessions.services import seed_stream_desired

            seed_stream_desired(binding)
            touched_ids.append(binding.pk)

    # Un-archive anything re-reported as open. The DERIVED staleness half of
    # `state=active` recomputes on every read, but this WRITTEN half does not heal
    # itself — without this, a task you reopened in emdash stays archived forever.
    #
    # A revival is also flagged, because the report cannot tell a REOPENED task
    # from a new task that reused a closed one's name — only the transcript the
    # next ship carries can (canopy_sessions.services.fork_if_name_reused).
    if touched_ids:
        from apps.canopy_sessions.services import REOPENED_KEY

        with transaction.atomic():
            for revived in Session.objects.select_for_update().filter(
                runner_binding__id__in=touched_ids, status=Session.ARCHIVED,
            ):
                revived.metadata = {**(revived.metadata or {}), REOPENED_KEY: True}
                revived.status = Session.ACTIVE
                revived.save(update_fields=["metadata", "status", "updated_at"])

    # Apply the closing signal. `now_keys` wins over `archived`: emdash task names are
    # not unique, so an open task must never be retired by an archived namesake.
    #
    # Still keyed on the NAME, unlike everything above, because that is all the wire
    # carries — `ReportSessionsIn.archived` is a list of strings with no project. So
    # the name-level guard stays: an archived `issues` under one repo cannot retire a
    # live `issues` under another, at the cost of not retiring it either. Erring
    # towards leaving a row open is the safe direction (staleness retires it on its
    # own clock); erring the other way deletes a live session from the web.
    closed = [k for k in (archived or []) if k and k not in now_keys]
    newly_closed: list = []
    if closed:
        closing = Session.objects.filter(
            runner_binding__runner=runner,
            runner_binding__session_key__in=closed,
        ).exclude(status=Session.ARCHIVED)
        newly_closed += list(closing.values_list("id", flat=True))
        closing.update(status=Session.ARCHIVED)

    # The other closing signal: absence from a COMPLETE report. emdash deletes a
    # task it closes (no row, no flag), so for an ordinary close the report's
    # silence is all there is — but it is a real observation when the report
    # arrived and held the runner's whole open set: a runner that cannot read
    # emdash sends no report at all, and a laptop asleep sends nothing, so
    # neither can reach this. Two complete reports in a row, to absorb a
    # flickering read. A wrong call heals itself: a task that is reported again
    # is un-archived above on the very next report.
    RunnerBinding.objects.filter(id__in=touched_ids).exclude(missed_reports=0).update(missed_reports=0)
    if complete:
        missing = (RunnerBinding.objects.filter(runner=runner)
                   .exclude(session_key="").exclude(id__in=touched_ids)
                   .exclude(session__status=Session.ARCHIVED))
        missing.update(missed_reports=F("missed_reports") + 1)
        gone = Session.objects.filter(runner_binding__in=missing.filter(missed_reports__gte=2))
        newly_closed += list(gone.values_list("id", flat=True))
        gone.update(status=Session.ARCHIVED)

    # A menu, unlike the runner FK below, MUST be cleared when its session stops
    # being reported. `pending_question` is only written inside the loop above,
    # which walks the sessions IN this report — so a session whose emdash task is
    # gone keeps its last dialog forever. Observed 2026-08-01: `7891` was absent
    # from emdash's open set entirely and the API still served a phone six
    # buttons, every one of which could only fail. The runner cannot fix this
    # from its side: it prunes its own copy, but it has no way to say anything
    # about a session it can no longer see.
    #
    # This is the same wholesale reconciliation the report already performs for
    # liveness — absence is a direct observation, not an inference — applied to
    # the one other field a report owns. It is NOT the same judgement as the FK
    # below: identity is durable and answers "which box", while a dialog is a
    # claim about a screen that no longer exists.
    # A close is satisfied by the task being gone, which is exactly what absence
    # from this wholesale report means. Clearing it here (rather than on a runner
    # ack) keeps one source of truth: the report already decides what is open.
    RunnerBinding.objects.filter(runner=runner, close_requested=True).exclude(
        id__in=touched_ids).update(close_requested=False)

    stale_menus = RunnerBinding.objects.filter(runner=runner).exclude(
        pending_question__isnull=True,
    ).exclude(id__in=touched_ids)
    for binding in stale_menus:
        menu_changes.append((binding.session_id, None))
    stale_menus.update(pending_question=None)

    # NOTHING is cleared here. `RunnerBinding.runner` is durable IDENTITY — which box
    # this session lives on — and a session must never forget that just because its
    # task stopped being reported (emdash DELETES a closed task, so falling off the
    # report is the NORMAL end of life, not an anomaly). Liveness is `live_seen_at`,
    # stamped above on everything in this report and read against
    # SESSION_LIVE_WINDOW; see apps/canopy_sessions/staleness.py.
    #
    # Nulling the FK here is what left labs with 47 sessions that were listed as
    # active, could not say which runner they came from, and had no way back.

    # Fire AFTER commit so apps/realtime fans the durable rows (never racing the DB)
    # to the runner-owner's supervisor group — the WS push that makes live emdash
    # activity reach every connected viewer at once. Local import avoids a cycle.
    def _fire_reported():
        from apps.harness.signals import sessions_reported

        sessions_reported.send(sender=Runner, runner=runner)

        # The dialog, to anyone already looking at that chat. Its own frame
        # rather than an overloaded `session.activity`: activity is "is the
        # agent producing", which the hook path owns and answers within a
        # tick — inventing a state here to carry a menu would mean reporting
        # an agent as idle or blocked on this path's much slower clock.
        from apps.realtime import groups

        for session_id, menu in menu_changes:
            groups.publish(groups.session_group(session_id),
                           {"type": "session.menu", "menu": menu})

        from apps.harness.signals import session_menu_changed

        for session_id, menu in menu_changes:
            try:
                session_menu_changed.send(sender=RunnerBinding, session_id=session_id, menu=menu)
            except Exception:  # noqa: BLE001 — a consumer must not break the report
                logger.exception("session_menu_changed receiver failed")

        # And to the phone in your pocket, for the agents that just STARTED
        # waiting. Only the null -> menu edge: a retraction is not news, and the
        # UI it would correct is already corrected by the frame above.
        #
        # This is the half no rendering fix could cover. A menu that renders
        # perfectly still needs somebody to open the app, and the failure being
        # fixed here is 52 minutes of nobody knowing there was anything to open.
        asking = new_asks
        if asking:
            from apps.canopy_sessions.models import Session
            from apps.push import services as push_services

            sessions = Session.objects.select_related(
                "agent", "runner_binding", "runner_binding__runner"
            ).in_bulk([sid for sid, _ in asking])
            for session_id, menu in asking:
                session = sessions.get(session_id)
                if session is not None:
                    push_services.notify_session_question(session, menu)

    transaction.on_commit(_fire_reported)
    if newly_closed:
        transaction.on_commit(lambda: fire_sessions_closed(newly_closed))
    return len(deduped)


@dataclass
class SessionView:
    """The wire projection of a live runner session — the fields EmdashSessionOut
    reads. Derived from Session + RunnerBinding; preserves the frozen shape."""

    id: str
    emdash_task: str
    project: str
    agent: "str | None"
    status: str
    last_interacted_at: object
    recent_messages: list
    workspace_id: str
    runner_name: str


def list_visible_sessions(user) -> list[SessionView]:
    """Open sessions in the caller's workspaces whose runner is LIVE. Newest-first.

    Three conditions, all polled or explicit — none of them "the FK went null":
      * the session is not explicitly ARCHIVED (a decision, effective immediately),
      * its binding was in a report within SESSION_LIVE_WINDOW (the polled clock) —
        or it is held by a cloud runner, which never re-reports a session,
      * and its runner is still heartbeating (Runner.live_status).
    The last two overlap by design: a runner that stops heartbeating also stops
    reporting, so the strictest of the two wins and a dead box's rows go quiet fast.

    A domain-matching teammate who has not been let into the workspace (an
    invite, or an approved access request) has no WorkspaceMembership row, so
    `user_workspace_slugs(user)` returns empty and their workspace's sessions
    are correctly invisible to them — no more silent auto-join here either.
    """
    from apps.canopy_sessions import access as session_access
    from apps.canopy_sessions.models import RunnerBinding, Session

    ws_slugs = wsvc.user_workspace_slugs(user)
    bindings = (
        RunnerBinding.objects.filter(
            runner__isnull=False,
            session__workspace_id__in=ws_slugs,
            # The chat ACL, not just the tenant: this returns each session's
            # recent messages.
            session__in=Session.objects.filter(session_access.visible_session_q(user)).values("pk"),
            session__status=Session.ACTIVE,
        )
        # The polled clock only means something on a box that reports. A cloud
        # runner records a session once per turn and never again, so there the
        # session stays open until it is closed (staleness.py's module note).
        .filter(
            Q(live_seen_at__gte=stale_cutoff())
            | Q(runner__kind=Runner.CLOUD, runner__sessions_reported_at__isnull=True)
        )
        .select_related("runner", "session", "session__agent")
        .order_by("-last_interacted_at")
    )
    out = []
    for b in bindings:
        if b.runner.live_status != Runner.ONLINE:
            continue
        out.append(
            SessionView(
                id=str(b.session_id),
                emdash_task=b.session_key,
                # `emdash_project`, not the raw column: a session targets an
                # agent XOR a project, so an agent-owned row has project="" and
                # reading it directly answered "" for every ACE run. The derived
                # value is the one that never moved — and it is the same half of
                # the identity a runner resolves a transcript by.
                project=b.session.emdash_project,
                agent=(b.session.agent.slug if b.session.agent_id else None),
                status=b.status,
                last_interacted_at=b.last_interacted_at,
                recent_messages=b.tail,
                workspace_id=b.session.workspace_id,
                runner_name=b.runner.name,
            )
        )
    return out

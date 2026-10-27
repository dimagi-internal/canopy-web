"""Harness domain services — the only write path for Runner/Turn/TurnEvent.

What lives here is a turn's lifecycle around the claim: enqueue (with the
capability gate on the way in; an email turn's sender and thread are resolved
by `channels.email`), pause, heartbeat (liveness only), mark running, finish,
cancel. All functions are synchronous and transaction-safe.

The rest is split by responsibility into its own modules — import each name from
the module that owns it: routing (`routing`), the claim and its explanations
(`claim`), the event ledger and transcripts (`ledger`), a runner's chat sessions
and their sync (`session_sync`), scheduled turns (`schedule_turns`), runner
administration (`runners`), readiness drills (`drills`) and the periodic sweeps
(`sweeps`).
"""
from __future__ import annotations

import datetime as dt
import logging
import uuid

from django.db import IntegrityError, transaction
from django.db.models import F
from django.utils import timezone

from apps.api.errors import TYPE_CONFLICT, ProblemError

from . import clis as cli_vocab
from . import sensitivity
from .channels import email as email_channel
from .claim import DEFAULT_LEASE_SECONDS
from .ledger import append_events
from .models import Runner, RunnerDrill, Turn, TurnEvent
from .routing import MIDTURN_FAILED

logger = logging.getLogger(__name__)

#: Every origin a turn may carry (`Turn.ORIGIN_CHOICES`).
ORIGINS = frozenset(value for value, _label in Turn.ORIGIN_CHOICES)


# How many times a turn that died BEFORE its session existed goes back on the queue
# before we give up and let it stay FAILED. Three is enough to ride out an emdash
# restart or a laptop waking up mid-drain; past that the fault is not transient and a
# turn that keeps churning is worse than one that stops and stays visible.
MAX_SESSIONLESS_RETRIES = 3


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

    agent = turn.acting_agent
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
        fields = ["status", "finished_at", "result_note"]
        if sensitivity.is_sensitive(turn):
            # Ended before it began, so `closing.close_turn` — where a sensitive
            # turn's prompt is otherwise wiped — never sees it: metadata only here
            # too. The note is canopy's own refusal, not anything the turn read.
            turn.prompt = ""
            fields.append("prompt")
        turn.save(update_fields=fields)
        # Refused whatever the rules say; the workspace's `offered-nothing` rule
        # decides what is recorded and who is told (canopy-web#1416).
        from apps.admission import evaluate as admission

        admission.at_refusal(turn, access="none")
        return
    turn.capability = cap
    turn.save(update_fields=["capability"])


class IdempotencyConflict(ProblemError):
    """The idempotency key already names a turn for a DIFFERENT target in this
    workspace.

    Keys are unique per workspace (`Turn.idempotency_scope`) and many are
    guessable (`sched:<id>:<slot>`, `task-<pk>-<i>`, `checkin-run:<pk>`), so a
    replay is honoured only for the target the key was made for; anything else
    is a 409 that says nothing about the turn holding the key."""

    def __init__(self) -> None:
        super().__init__(
            409, "Idempotency key in use", type_=TYPE_CONFLICT,
            detail="this idempotency_key already names a different turn; use a fresh key",
        )


def _replay(existing: Turn, *, agent, project, session, workspace) -> Turn:
    """The turn a retry with the same key returns — only when it targets exactly
    what this request targets (same agent, same session, or same project in the
    same workspace). Else IdempotencyConflict."""
    if agent is not None:
        same = existing.agent_id == agent.pk
    elif session is not None:
        same = existing.chat_session_id == session.pk
    else:
        same = (bool(existing.project) and existing.project == project
                and workspace is not None and existing.workspace_id == workspace.pk)
    if not same:
        raise IdempotencyConflict()
    return existing


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
    authserv_id: str | None = None,
    requested_cli: str = "",
) -> tuple[Turn, bool]:
    """Queued turns stack freely — the executing-turn index never blocks intake
    (new turns are born `queued`, which the index does not cover).

    Targets exactly one of agent / project / session. A project turn must carry a
    workspace: it has no agent/session to derive tenancy from, and claim_next_turn
    fails it closed without one, so accepting it here would silently queue a turn
    nothing can ever run. Session turns derive tenancy from session.workspace.

    `authserv_id` — server-side callers only (apps/mail): the receiver whose
    Authentication-Results this mail is graded on. Never taken from a request.

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
    # And the one check every producer meets: `origin` is what routing rules, the
    # caller envelope and the access rule key on, so a value outside the vocabulary
    # (a typo, a stored spec from a retired producer) is refused here rather than
    # written as a turn nothing routes and no rule recognises.
    if origin not in ORIGINS:
        raise ValueError(f"unknown turn origin {origin!r}; one of {sorted(ORIGINS)}")
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
        # Mail names its own asker and conversation (apps/harness/channels/email.py):
        # who wrote in, refused at the door when blocked, bound to its thread's session.
        arrival = email_channel.arrive(
            agent, origin_ref, initiator=initiator, session=session, authserv_id=authserv_id,
            origin=origin, idempotency_key=idempotency_key, prompt=prompt, routing=routing)
        if arrival.refusal is not None:
            return arrival.refusal
        agent, session, initiator = arrival.agent, arrival.session, arrival.initiator
    if sum([bool(agent), bool(project), bool(session)]) != 1:
        raise ValueError("a turn targets exactly one of agent / project / session")
    if project and workspace is None:
        raise ValueError("a project turn needs a workspace")
    # Looked up inside the target's workspace: another workspace holding the
    # same key is no business of this one (and cannot pre-claim it).
    scope = Turn.scope_for(agent=agent, session=session, workspace=workspace)
    existing = Turn.keyed(idempotency_key, scope).first()
    if existing is not None:
        return _replay(existing, agent=agent, project=project, session=session,
                       workspace=workspace), False
    if initiator is None:
        # A caller that says nothing gets `unknown`, not a guess from
        # `enqueued_by`: that field is the CALLER, which for a runner-posted turn
        # is the wrong person. `tests/test_turn_initiator.py` fails on any
        # production path that still lands here.
        from . import initiator as who
        initiator = who.unknown(via=origin)
    # An agent's turn login working for a person (turn_asker): every turn it starts
    # carries them, whichever door it came through — a dispatch, a send into another
    # session, a new session. The server's record only; a client's is dropped at the door.
    from . import turn_asker  # noqa: PLC0415 — cycle: turn_asker -> turn_agent_tokens -> services

    on_behalf = turn_asker.of(getattr(initiator, "user", None))
    if on_behalf is not None:
        origin_ref = {**(origin_ref or {}), "on_behalf_of": on_behalf.record()}
    from apps.threads import guard as thread_guard

    from . import provenance

    try:
        with transaction.atomic():
            if thread_guard.is_thread_message(origin_ref):
                # An agent-thread message: refused (ThreadRefused, an HttpError)
                # unless the thread is open, in time, within budget, spoken by a
                # participant as itself, and next in order. After the idempotency
                # check above, so a retried message returns its turn; inside this
                # transaction, so the thread row stays locked until the turn exists.
                target = (agent.slug if agent is not None
                          else session.agent.slug if session is not None and session.agent_id
                          else "")
                thread_guard.check_message(origin_ref, target, idempotency_key)
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
                idempotency_scope=scope,
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
                # The CLI its starter asked for, already checked by the caller
                # (`clis.refusal`); blank lets the claim pick from the agent's list.
                requested_cli=requested_cli,
            )
            if session is not None:
                # A turn started from a sensitive turn or conversation makes the
                # conversation it joins sensitive before anything of it lands
                # there, so what is sealed and who reads it back agree.
                sensitivity.take_on(session, turn)
            _apply_capability(turn, capability)
            # A combination that can never complete is refused here, before any
            # runner can see it — or, when the rule needs the runner or the
            # resolved mode, at the claim (apps/admission/evaluate.py).
            from apps.admission import evaluate as admission

            admission.at_enqueue(turn)
    except IntegrityError:
        # Only possible race: same idempotency key inserted concurrently.
        replay = Turn.keyed(idempotency_key, scope).first()
        if replay is not None:
            return _replay(replay, agent=agent, project=project, session=session,
                           workspace=workspace), False
        raise
    if origin == Turn.ORIGIN_EMAIL:
        email_channel.log_unproven_member(turn)
    if session is not None:
        # New work on the session (apps/push drops any pending "done" push).
        from .signals import session_turn_enqueued

        session_turn_enqueued.send(sender=Turn, turn=turn, session_id=session.pk)

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


_PAUSE_FIELDS = ["paused", "paused_note", "paused_at", "paused_by", "unpause_at"]


def pause(runner: Runner, *, note: str = "", until: dt.datetime | None = None, by=None) -> None:
    """Park a runner: the one write every pause makes (/pause from the UI, MCP,
    Ada, and the runner's own sentinel, which calls /pause too).

    `until` schedules the unpause. It never weakens a stronger pause: it is not
    added to an open-ended one, and a second cap only moves it later. A pause
    without `until` cancels any scheduled unpause — the operator outranks the
    clock. A `until` already past parks nothing.

    A pause that ends up open-ended strands every queued turn only this box
    could take, so those are put to the admission rules now (after commit)
    rather than left queued for a resume nobody scheduled.
    """
    now = timezone.now()
    if until is not None and until <= now:
        return  # the reset has already passed — nothing to park
    note = (note or "")[:200]
    if not runner.paused:
        runner.paused_at = now
        runner.unpause_at = until
    elif runner.unpause_at is None:
        if until is not None:
            return  # an operator's open-ended pause; keep it and its note
    else:
        runner.unpause_at = None if until is None else max(until, runner.unpause_at)
    runner.paused_note = note
    runner.paused_by = by if getattr(by, "is_authenticated", False) else None
    runner.paused = True
    runner.save(update_fields=_PAUSE_FIELDS)
    if runner.unpause_at is None:
        runner_pk = runner.pk

        def _strand_check():
            from apps.admission import evaluate

            evaluate.recheck_stranded(Runner.objects.get(pk=runner_pk))

        transaction.on_commit(_strand_check)


def unpause(runner: Runner) -> None:
    """Lift a runner's pause and cancel any scheduled unpause. The one write both
    /unpause and the scheduled wake make."""
    if not runner.paused and runner.unpause_at is None:
        return
    runner.paused = False
    runner.paused_note = ""
    runner.paused_at = None
    runner.paused_by = None
    runner.unpause_at = None
    runner.save(update_fields=_PAUSE_FIELDS)


def wake_due_runners(now: dt.datetime) -> set:
    """Run every scheduled unpause (`Runner.unpause_at`) that has come due, and
    return the ids woken.

    A row-locked re-check per runner, so an operator who re-paused the box
    open-ended (which cancels `unpause_at`) between the query and the write is
    never overridden, and two concurrent beats wake it once.
    """
    woken = set()
    due = Runner.objects.filter(paused=True, unpause_at__lte=now).values_list("pk", flat=True)
    for pk in list(due):
        with transaction.atomic():
            runner = Runner.objects.select_for_update().filter(pk=pk).first()
            if runner is None or not runner.paused or runner.unpause_at is None \
                    or runner.unpause_at > now:
                continue
            logger.info("pause: scheduled unpause of runner %s (%s) — was: %s",
                        runner.name, runner.pk, runner.paused_note)
            unpause(runner)
            woken.add(runner.pk)
    return woken


#: How long a runner may keep renewing a turn a person asked to stop. Long enough
#: for a stop to land on a box whose socket is reconnecting (the heartbeat reply
#: re-delivers it every beat), short enough that a runner which cannot stop a turn
#: does not hold it — and the agent's one executing slot — indefinitely.
CANCEL_GRACE_SECONDS = 300


def _valid_turn_ids(ids) -> list[uuid.UUID]:
    out = []
    for raw in ids or []:
        try:
            out.append(uuid.UUID(str(raw)))
        except (ValueError, TypeError):
            continue
    return out


def cancel_requested_turn_ids(runner: Runner, active_turn_ids) -> list[str]:
    """The turns this runner reports running that a person has asked to stop.

    The DURABLE half of a stop. `cancel_turn` publishes `runner.cancel` down the
    runner's socket, but a frame is a doorbell: a box on the REST fallback, or
    whose socket is mid-reconnect, never hears it, and the turn ran to completion
    (cloud-ec2-2, 2026-10-17). The `cancel_requested` ledger event is the record,
    and every heartbeat reply now carries it back, so the stop lands within one
    beat on every path. Both runner kinds read the same field.
    """
    ids = _valid_turn_ids(active_turn_ids)
    if not ids:
        return []

    executing = Turn.objects.filter(
        pk__in=ids, claimed_by=runner,
        status__in=[Turn.CLAIMED, Turn.RUNNING, Turn.NEEDS_HUMAN],
    )
    asked = TurnEvent.objects.filter(
        turn__in=executing, kind="cancel_requested").values_list("turn_id", flat=True)
    return sorted({str(t) for t in asked})


def _cancel_overdue_ids(active_turn_ids, now) -> list[uuid.UUID]:
    """Reported turns whose FIRST stop request is older than CANCEL_GRACE_SECONDS."""
    ids = _valid_turn_ids(active_turn_ids)
    if not ids:
        return []
    cutoff = now - dt.timedelta(seconds=CANCEL_GRACE_SECONDS)
    return list(
        TurnEvent.objects.filter(turn_id__in=ids, kind="cancel_requested", ts__lt=cutoff)
        .values_list("turn_id", flat=True).distinct()
    )


def heartbeat(
    runner: Runner, *, active_turn_ids: list[str], degraded: bool = False, note: str = "",
    ready: bool = True, ready_note: str = "", code_branch: str = "",
    code_version: str = "", code_sha: str = "", code_committed_at: int = 0,
    projects: list[str] | None = None, profiles: int = 0,
    health: dict | None = None, mailboxes_readable: list[str] | None = None,
    envelope: int = 0, midturn: int = 0, agents_unprepared: list[str] | None = None,
    data_mode: str = "", data_mode_hash: str = "", claude_model: str = "",
    clis: dict | None = None,
) -> Runner:
    """A runner's liveness and what it reports, nothing else: the beat renews the
    leases of the turns it holds and answers with the stops it owes. Periodic
    work that used to ride the beat (scheduled unpauses, due pushes, purges,
    notices) runs on the off-request clock, apps/common/ticks.py, so it does not
    stop when no runner is beating.

    `profiles` is the profile-enforcement version the runner REPORTS it can
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
    stored list (and its `mailboxes_checked_at`) alone. So does
    `agents_unprepared` (`capabilities["unprepared"]`, read by
    `routing.cannot_serve`).
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
    if agents_unprepared is not None:
        from .routing import UNPREPARED

        unprepared = sorted({s.strip() for s in agents_unprepared if s and s.strip()})
        if unprepared != (runner.capabilities.get(UNPREPARED) or []) or (
                UNPREPARED not in runner.capabilities):
            runner.capabilities = {**runner.capabilities, UNPREPARED: unprepared}
            if "capabilities" not in fields:
                fields.append("capabilities")
    if health is not None:
        # `bootstrapped_at` never goes backwards: a laptop reports health on every
        # beat now (its updater's check), and one that restarted before its next
        # refresh sends 0 — which would reopen a Refresh it already discharged.
        try:
            booted = max(float(health.get("bootstrapped_at") or 0),
                         float((runner.health or {}).get("bootstrapped_at") or 0))
        except (TypeError, ValueError):
            booted = float(health.get("bootstrapped_at") or 0)
        runner.health = {**health, "bootstrapped_at": booted, "received_at": now.isoformat()}
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
    # Which agent CLIs the box has (`harness.clis`). A level, like `envelope`:
    # written every beat, and a beat without it removes it, so a box whose code
    # stops reporting a CLI is read as Claude Code alone on its next beat.
    reported_clis = cli_vocab.sanitize(clis)
    unknown = cli_vocab.unknown_names(clis)
    if (runner.capabilities.get("clis") != reported_clis
            or (runner.capabilities.get("clis_unknown") or []) != unknown):
        caps = {k: v for k, v in runner.capabilities.items() if k not in ("clis", "clis_unknown")}
        if reported_clis is not None:
            caps["clis"] = reported_clis
        if unknown:
            # Shown on the runner's page, never routed to: a box reporting only
            # these takes no turns, and says why (`RunnerOut.agent_clis`).
            caps["clis_unknown"] = unknown
        runner.capabilities = caps
        if "capabilities" not in fields:
            fields.append("capabilities")
    # The model the box's sessions start on (`opus[1m]`), for the chat's context
    # meter: a display hint, never a routing input. Same contract, absent = "".
    model = (claude_model or "")[:64]
    if (runner.capabilities.get("claude_model") or "") != model:
        runner.capabilities = {**runner.capabilities, "claude_model": model}
        if "capabilities" not in fields:
            fields.append("capabilities")
    # The box's data mode and its config hash (`runner_requirements.in_force`):
    # reported, same contract as `envelope` -- written every beat, absent = "", so a
    # box that stops running in sensitive-data mode loses its `sensitive_data` on the next beat.
    from . import runner_requirements as rr

    for key, value in ((rr.DATA_MODE_KEY, (data_mode or "")[:32]),
                       (rr.DATA_MODE_HASH_KEY, (data_mode_hash or "")[:64])):
        if (runner.capabilities.get(key) or "") != value:
            runner.capabilities = {**runner.capabilities, key: value}
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
    # The stops this runner owes: reported turns a person asked to stop. Rides
    # the reply (RunnerOut.cancel_turn_ids / the WS heartbeat.ack) so a stop
    # reaches the box even when the `runner.cancel` frame did not — see
    # `cancel_requested_turn_ids`.
    runner.cancel_turn_ids = cancel_requested_turn_ids(runner, active_turn_ids)
    if active_turn_ids:
        # A turn whose stop has gone unhonoured past the grace is NOT renewed: the
        # runner owns the lease, but "owns" cannot mean "may hold a stopped turn
        # forever by heartbeating it". The sweep then closes it CANCELLED (its
        # cancel_requested rule), freeing the agent's one executing slot.
        overdue = _cancel_overdue_ids(active_turn_ids, now)
        Turn.objects.filter(
            pk__in=active_turn_ids,
            claimed_by=runner,
            status__in=[Turn.CLAIMED, Turn.RUNNING, Turn.NEEDS_HUMAN],
        ).exclude(pk__in=overdue).update(
            lease_expires_at=now + dt.timedelta(seconds=DEFAULT_LEASE_SECONDS))
        # A rider lives exactly as long as the turn it rode into: renewed with
        # it, so it can only be swept when that turn is.
        Turn.objects.filter(
            rides_turn_id__in=active_turn_ids,
            claimed_by=runner,
            status__in=[Turn.CLAIMED, Turn.RUNNING, Turn.NEEDS_HUMAN],
        ).exclude(rides_turn_id__in=overdue).update(
            lease_expires_at=now + dt.timedelta(seconds=DEFAULT_LEASE_SECONDS))
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
    # Whatever happens below — terminal, or back to the queue — the turn stops
    # executing on its runner, so the agent logins issued for it die now.
    from .turn_agent_tokens import revoke_for_turn

    revoke_for_turn(turn.pk)
    if sensitivity.is_sensitive(turn):
        result_note = sensitivity.withheld_note(result_note)  # every path below stores it

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
                 started_at=None, rides_turn=None, origin_ref=ref, result_note=result_note,
                 cli="")
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
            # Picked again at the next claim: no session was made, so nothing
            # holds it to the CLI that failed to start (`clis.allowed`).
            cli="",
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
    if status == Turn.DONE and turn.cli:
        # It ran to the end on this CLI, so its conversation's history is there,
        # whether or not the box ever reported the session (`clis.pin_conversation`).
        cli_vocab.pin_conversation([turn.chat_session_id, turn.canopy_session_id], turn.cli)
    from_states = [Turn.CLAIMED, Turn.RUNNING, Turn.NEEDS_HUMAN]
    if allow_queued:
        from_states.append(Turn.QUEUED)
    # A drill turn that ends without its agent's report resolves its RunnerDrill
    # here — the agent may never get far enough to curl the callback at all, and
    # left alone the drill strands OUTCOME_PENDING forever (close_turn only
    # touches a still-pending drill, so one already reported is never clobbered):
    #
    # * FAILED: auth expired, environment broken.
    # * CANCELLED: drills queue behind real executing turns (start_drill's
    #   docstring) and the plain /turns/{id}/cancel route has no origin filter,
    #   so a queued drill can be cancelled out from under itself.
    # * `stop_ignored`: such a turn USED to arrive here as CANCELLED, before the
    #   DONE -> CANCELLED coercion went away. The drill's resolution depends on
    #   "a stop was requested", not on which label the turn ended up with.
    # * DONE without a report: the report IS the drill's answer, and "the turn
    #   ran but the callback never arrived" is exactly the control-plane gap a
    #   drill exists to catch (drill 50 on cloud-ec2-1, whose report had 404'd).
    #   The agent reports DURING its turn, so any report has already landed.
    #
    # MISSED is the one status that leaves a drill alone.
    if status == Turn.DONE and not stop_ignored:
        drill_summary = "drill turn finished without reporting"
        if result_note:
            drill_summary += f" — the agent said: {result_note[:1500]}"
    elif stop_ignored:
        drill_summary = "drill turn completed after a cancel that never landed"
    elif status == Turn.CANCELLED:
        drill_summary = "drill turn cancelled"
    elif status == Turn.FAILED:
        drill_summary = result_note or "drill turn failed"
    else:
        drill_summary = None
    from .closing import close_turn

    # close_turn also tells the debugger about a turn that really ENDED failed —
    # one requeued sessionless, or a rider sent back to wait, returned above.
    if not close_turn(turn, status, result_note, from_states=from_states,
                      drill_summary=drill_summary):
        return turn
    if stop_ignored:
        # SAY IT WHERE THE PERSON WHO PRESSED STOP IS LOOKING. A `status` event
        # carries no client-visible frame (stream_map.turn_event_to_frames), so the
        # truthful DONE above would otherwise be indistinguishable, on screen, from a
        # turn nobody tried to stop. `error` renders as chat.stream_error.
        append_events(turn, [{"kind": "error", "payload": {
            "detail": "your stop never reached the runner — this turn ran to completion",
        }}])
    return turn


def _finish_riders(turn: Turn, status: str | None = None) -> None:
    """The follow-ups delivered into `turn` end when it does (`closing.close_riders`)."""
    from .closing import close_riders

    close_riders(turn, status)


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
        from .closing import close_turn

        # A queued drill cancelled this way must not strand OUTCOME_PENDING.
        if close_turn(turn, Turn.CANCELLED, "cancelled", from_states=[Turn.QUEUED],
                      event=who, drill_summary="drill turn cancelled"):
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

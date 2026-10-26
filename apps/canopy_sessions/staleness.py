"""The liveness window, defined once.

Separate from services.py so it can be imported without dragging in models, signals,
and the realtime bridge — including from a migration, which imports SESSION_LIVE_WINDOW
(the window is data) but must NOT reuse `unseen_q` (the rule is logic, and it moves).
Migration 0009 learned that the hard way: the predicate grew a join into
`Runner.sessions_reported_at`, a field its historical models do not have, and every
test database stopped building. A one-shot backfill freezes its own copy of the rule.
"""
from __future__ import annotations

import datetime as dt

from django.apps import apps
from django.db.models import (
    BooleanField,
    Case,
    CharField,
    Exists,
    ExpressionWrapper,
    F,
    OuterRef,
    Q,
    Subquery,
    Value,
    When,
)
from django.db.models.functions import Coalesce
from django.db.models.lookups import Exact
from django.utils import timezone

# How long a runner-discovered session survives with no runner sighting before it
# drops out of `state=active`.
#
# LIVENESS IS POLLED, NOT EVENTED. The runner re-reports its ENTIRE open-task set on
# a guaranteed heartbeat every `session_report_seconds` (10s) — not only when
# something changes — and skips the report outright if it could not read emdash. So
# "seen in the last few minutes" is a direct, self-healing observation of what is
# actually open on that box, and absence needs no explanation to be acted on.
#
# It replaced a 3-day window that existed to cover an ambiguity that turned out to be
# unfixable at the source: the written closing signal (`list_recently_archived_tasks`,
# `archived_at IS NOT NULL`) NEVER FIRES, because emdash DELETES a closed task rather
# than archiving it — verified 2026-07-25 on a 7-week-old emdash4.db with zero
# archived rows ever. Every closed task therefore fell through to the 3-day timer, and
# labs accumulated 47 sessions that were `active`, unreachable, and (because the old
# report path nulled their runner FK) could not even say which box they came from.
#
# 3 minutes is ~18x the report cadence, so a runner restart or a run of swallowed
# POSTs never retires anything, while a box that has genuinely gone away stops being
# offered promptly. The rule is derived on every read, so a returning runner
# un-retires its sessions with no repair step.
SESSION_LIVE_WINDOW = dt.timedelta(minutes=3)

# A box that posts NO wholesale reports (the cloud runner) has no open-task set to
# observe: it runs each turn as one Claude process that EXITS when the turn ends. So
# there, a finished turn says nothing about whether the SESSION is done — a manual
# turn ends by asking for approval, and its session is exactly the one you need to
# find. #1140 closed a cloud session the moment its last turn ended; that hid every
# session waiting on a person (Jonathan, 2026-10-07: "the sessions weren't done").
# So a cloud-held session stays open until someone closes or archives it, or its
# runner is retired (the box is gone; nothing can continue it there). The list shows
# each session's turn mode, which is how an auto session that just ran and a manual
# one waiting on you are told apart.
#
# Except a session that RAN ON ITS OWN (`feed.ran_on_its_own`: started in auto mode,
# not from a person's chat): nobody is waiting on it by definition, so once nothing
# on it is in flight it is done. Left open, these were 56 of Jonathan's 62 "active"
# sessions on 2026-10-11 — finished huddle rounds, thread messages and scheduled
# turns, burying the six that needed a person (#1396). Manual and chat-started
# sessions keep the open-until-closed rule above, and a session with a dialog up
# (`pending_question`) is never retired whatever its mode.

#
# And a PERSON's web chat on the cloud closes once it has been idle for
# CLOUD_CHAT_IDLE: no turn on it created or finished in that long, nothing in
# flight, no dialog up, started in auto mode. Held open until closed, these were
# 106 of cloud-ec2-2's 110 open sessions on 2026-10-26, none needing anyone —
# a web chat left alone is far more often abandoned than waiting (Jonathan,
# 2026-10-26). Sending to it again is a new turn, which reopens it. A session an
# AGENT started (its first turn's initiator is an agent, or an agent's own login)
# ran on its own whatever channel it came in on.
CLOUD_CHAT_IDLE = dt.timedelta(hours=24)

# How long a transferred session stays listed while its NEW box has not reported it
# yet. A transfer clears the source's identity off the binding, waits up to
# `handoff.CAPTURE_WAIT` (3 min) for the source to pack the worktree, and only then
# enqueues the turn the target has to claim — so the last sighting is the SOURCE's
# and goes stale before the target's first report, and the session vanished from
# every list mid-move and came back minutes later. Bounded, so a transfer whose
# target never picks it up still retires like any other quiet session.
TRANSFER_WINDOW = dt.timedelta(minutes=30)


def transferring_q(now=None) -> Q:
    """Moved onto another box recently, and not yet seen there."""
    since = (now or timezone.now()) - TRANSFER_WINDOW
    return (
        Q(runner_binding__transferred_at__isnull=False)
        & Q(runner_binding__transferred_at__gte=since)
        & (
            Q(runner_binding__live_seen_at__isnull=True)
            | Q(runner_binding__live_seen_at__lt=F("runner_binding__transferred_at"))
        )
    )


def is_transferring(binding, now=None) -> bool:
    """`transferring_q` for one binding — the list's "Transferring…" flag."""
    if binding is None or binding.transferred_at is None:
        return False
    if binding.transferred_at < (now or timezone.now()) - TRANSFER_WINDOW:
        return False
    return binding.live_seen_at is None or binding.live_seen_at < binding.transferred_at


def driving_turns():
    """The turns that drove a session, for use under a Session queryset: a chat
    send carries the session itself (`chat_session`), an agent turn the runner
    opened a session for carries the session too (`Turn.canopy_session`, written
    by record-session) and its key (`Turn.session_key`, the binding's).
    Defined once for `transcript_read.with_driving_turn` (the mode badge and the feed)
    and `unseen_q` (the auto-retire leg), so the two cannot disagree about which
    turn started a session.

    The key alone is not enough: a dispatched turn that carries its own thread key
    (a huddle round) can leave the turn's key and the binding's different, and then
    the session had no driving turn at all — no mode badge, and on the cloud runner
    never retired."""
    Turn = apps.get_model("harness", "Turn")
    return Turn.objects.filter(
        Q(chat_session=OuterRef("pk"))
        | Q(canopy_session=OuterRef("pk"))
        | (
            Q(agent=OuterRef("agent"), session_key=OuterRef("runner_binding__session_key"))
            & ~Q(session_key="")
        )
    )


def stale_cutoff(now=None):
    """The live_seen_at floor for `state=active`. A binding last seen before this is
    treated as archived — derived, never written, so it un-archives itself the moment
    the task is reported again."""
    return (now or timezone.now()) - SESSION_LIVE_WINDOW


def unseen_q() -> Q:
    """Sessions no runner is currently reporting — the derived half of `state=active`.

    Where a chat was STARTED says nothing about whether it is still open. Once a web
    chat has been sent, a runner holds an emdash session for it and re-reports it every
    ~10s exactly like a runner-discovered one, so absence from that report is the same
    direct observation for both. Scoping the rule to `origin="runner"` left labs listing
    11 week-old phone chats as live whose emdash tasks had been deleted days earlier
    (2026-07-31) — indistinguishable, in the list, from a chat you could continue.

    But staleness needs an OBSERVER, and that is what origin was standing in for badly.
    Two things have to be true before absence means anything: a runner holds this
    session, and that runner is one that posts wholesale reports. An unbound web chat
    fails the first (no runner yet) and is retired only by an explicit archive. A
    cloud-held chat fails the second, so it has its own observation: whether a turn
    on it is in flight.

    Being wrong in the archiving direction is cheap and self-healing — the rule is
    derived on every read, so one report brings a session back — but it is not free:
    the failure is a live chat vanishing from the list, so the gates above are real.
    """
    p = _predicates()
    return (p.runner_unseen | p.web_unseen) & ~p.transferring


class _Predicates:
    """The named checks `unseen_q` is built from, so `open_reason` can say which
    one holds a session open without restating any of them."""


def _predicates() -> _Predicates:
    p = _Predicates()
    # Its binding stopped being reported. `live_seen_at__isnull=True` also covers
    # having no binding at all (the LEFT JOIN yields NULL), which is why the web leg
    # below requires one explicitly.
    quiet = Q(runner_binding__live_seen_at__lt=stale_cutoff()) | Q(
        runner_binding__live_seen_at__isnull=True
    )

    # UNCHANGED. A runner-discovered session could only have come from a report, so
    # absence — of a sighting or of a binding entirely — is staleness with no further
    # qualification. Narrowing this leg would resurrect the 47 zombies of 2026-07-25.
    #
    # Except on the cloud runner, which records a session per turn and never reports
    # it again: there, the session is open until it is closed or its runner is
    # retired (see the module note above `stale_cutoff`). Gated on the runner's KIND, not on
    # its never having reported: a laptop that went quiet before
    # `sessions_reported_at` existed has no stamp either, and its sessions are
    # exactly the zombies this leg retires. A session with no binding, or whose
    # binding lost its runner, keeps the rule above.
    unobserved = Q(runner_binding__runner__kind="cloud") & Q(
        runner_binding__runner__sessions_reported_at__isnull=True
    )
    Runner = apps.get_model("harness", "Runner")
    cloud_gone = Q(runner_binding__runner__status=Runner.RETIRED)
    # A cloud-held session that ran on its own is done once nothing on it is in
    # flight (the module note above `driving_turns`). Its mode is the FIRST claimed
    # turn's — the same turn `with_driving_turn` badges — and a later turn still
    # queued or running keeps it open. A dialog up keeps it open too.
    Turn = apps.get_model("harness", "Turn")
    driving = driving_turns().exclude(turn_mode="").order_by("created_at")
    # Coalesced: a session with no claimed turn yet must compare FALSE, not NULL —
    # `state=active` EXCLUDES this predicate, and NOT(NULL) would drop the row.
    def first(field):
        return Coalesce(Subquery(driving.values(field)[:1]), Value(""))

    # Started by an agent: the first turn's initiator is one, or is an agent's own
    # login (an agent calling the API as itself records `kind="user"`).
    Agent = apps.get_model("agents", "Agent")
    by_agent = driving.annotate(_by_agent=ExpressionWrapper(
        Q(initiator_kind="agent") | Q(Exists(Agent.objects.filter(user=OuterRef("initiator_user")))),
        output_field=BooleanField(),
    ))
    started_by_agent = Coalesce(Subquery(by_agent.values("_by_agent")[:1]), Value(False))
    auto = Q(Exact(first("turn_mode"), "auto"))
    web_chat = Q(Exact(first("origin"), Turn.ORIGIN_CANOPY_WEB_CHAT))
    ran_on_its_own = auto & (~web_chat | Q(Exact(started_by_agent, True)))
    # Idle: no driving turn created or finished within CLOUD_CHAT_IDLE. Turn times,
    # not the binding's: a cloud binding's `live_seen_at` is stamped per turn and
    # its `last_interacted_at` is never reported.
    idle_cutoff = timezone.now() - CLOUD_CHAT_IDLE
    recent = driving_turns().filter(Q(created_at__gte=idle_cutoff) | Q(finished_at__gte=idle_cutoff))
    chat_idle = auto & web_chat & ~Q(Exists(recent))
    in_flight = Exists(driving_turns().filter(status__in=Turn.NON_TERMINAL))
    no_dialog = Q(runner_binding__pending_question__isnull=True) | Q(
        runner_binding__pending_question={}
    )
    cloud_finished = (ran_on_its_own | chat_idle) & ~Q(in_flight) & no_dialog
    cloud_done = cloud_gone | cloud_finished

    runner_unseen = Q(origin="runner") & (
        (~unobserved & quiet) | (unobserved & cloud_done)
    )

    # NEW, and gated on an OBSERVER existing. A sent web chat is held and reported
    # like any other, so its going quiet means the same thing — but ONLY on a box that
    # actually posts wholesale reports. The cloud runner does not (it record-sessions
    # once per turn and has no open-task set to report), so there `live_seen_at` is a
    # creation stamp and its age is not evidence. Ungated, this archived a cloud chat
    # 3 minutes after its turn STARTED, mid-run. `sessions_reported_at` is that gate; see Runner for why it is `isnull`
    # rather than a freshness window.
    #
    # And held by the cloud runner, the same rule the runner leg uses: open until it
    # is closed, its runner is retired, or (a person's auto-mode chat) it has been
    # idle for CLOUD_CHAT_IDLE. (Labs once listed a hal chat 67 days after
    # its last word, on a cloud runner long since gone — the retired gate ends that.)
    web_unseen = (
        ~Q(origin="runner")
        & Q(runner_binding__isnull=False)
        & (
            (Q(runner_binding__runner__sessions_reported_at__isnull=False) & quiet)
            | (unobserved & cloud_done)
        )
    )

    # A session mid-transfer is unseen by design (see TRANSFER_WINDOW), not gone.
    p.quiet, p.unobserved, p.cloud_gone = quiet, unobserved, cloud_gone
    p.first_mode, p.first_origin = first("turn_mode"), first("origin")
    p.in_flight, p.no_dialog = in_flight, no_dialog
    p.runner_unseen, p.web_unseen = runner_unseen, web_unseen
    p.transferring = transferring_q()
    return p


# Why an open session is open: one fixed label per leg of `unseen_q` that can keep
# a session out of it, checked in this order, first match wins. Each is a condition
# on stored rows (no inference), so the same data always gives the same reason.
OPEN_REASONS = (
    "transferring",       # moved to another box within TRANSFER_WINDOW, not seen there yet
    "reported_recently",  # a reporting runner named it within SESSION_LIVE_WINDOW
    "turn_running",       # cloud: a turn that drives it is queued, claimed or running
    "dialog_up",          # cloud: a question is waiting for an answer
    "manual_turn",        # cloud: its first claimed turn ran in manual mode
    "person_started",     # cloud: a person's web chat with a turn within CLOUD_CHAT_IDLE
    "no_turn_found",      # cloud: no claimed turn drives it, so nothing can finish it
    "unbound",            # a web chat no runner has picked up yet
    "unobservable",       # held by a box that has never sent a session report
)
#: An open session no reason above matches: the rule and this explanation disagree.
UNCLASSIFIED = "unclassified"


def open_reason():
    """An expression for `.annotate()` on an `active()` queryset: which of
    OPEN_REASONS holds each session open. Built from `_predicates()`, the same
    checks `unseen_q` excludes on, so the two cannot drift; UNCLASSIFIED means they
    have (`tests/test_open_session_reasons.py` pins that it never happens)."""
    Turn = apps.get_model("harness", "Turn")
    p = _predicates()
    runner = Q(origin="runner")
    reporting = (runner & ~p.unobserved) | (
        ~runner & Q(runner_binding__runner__sessions_reported_at__isnull=False)
    )
    cloud = p.unobserved & ~p.cloud_gone
    bound = Q(runner_binding__isnull=False)
    whens = [
        When(p.transferring, then=Value("transferring")),
        When(reporting & bound & ~p.quiet, then=Value("reported_recently")),
        When(cloud & Q(p.in_flight), then=Value("turn_running")),
        When(cloud & ~p.no_dialog, then=Value("dialog_up")),
        When(cloud & Q(Exact(p.first_mode, "manual")), then=Value("manual_turn")),
        When(cloud & Q(Exact(p.first_origin, Turn.ORIGIN_CANOPY_WEB_CHAT)),
             then=Value("person_started")),
        When(cloud & Q(Exact(p.first_mode, "")), then=Value("no_turn_found")),
        When(~runner & ~bound, then=Value("unbound")),
        When(~runner & bound & ~reporting & ~p.unobserved, then=Value("unobservable")),
    ]
    return Case(*whens, default=Value(UNCLASSIFIED), output_field=CharField())


def active(sessions):
    """`state=active`: open, and still being observed — the ONE rule for "open
    sessions", read by the session list and the per-runner count alike."""
    return sessions.filter(status="active").exclude(unseen_q())


def archive_stale_sessions(session_model) -> int:
    """WRITE the derived rule down: archive every session `unseen_q` currently matches.
    Returns the number of rows changed.

    Not needed for the list to read correctly — `unseen_q` is applied on every read, so
    a stale session is already excluded without this. It exists as a repair/one-shot
    utility. Takes the model class rather than importing one, so a caller can hand it a
    historical model; note that only works while the predicate stays within fields that
    model has (see the module docstring — migration 0009 no longer uses this).
    """
    return (
        session_model.objects.filter(status="active")
        .filter(unseen_q())
        .update(status="archived")
    )

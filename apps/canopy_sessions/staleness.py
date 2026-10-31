"""Which sessions are open, and which turns drive a session.

`active` is the one rule for "open sessions": `status="active"` minus `unseen_q`,
which is derived on every read from the binding's sightings and, on a box that
posts no wholesale session report (the cloud runner), from the turns that drive
the session. `driving_turns` / `driving_turns_of` define "the turns that drive a
session" once, for this module, the mode badge (`transcript_read.with_driving_turn`),
the session resolver (`session_state`), placement and the session info page.
`ran_on_its_own` is the one rule for "nobody is waiting on this session".

A migration may import the window constants (data) but must not reuse the
predicates (logic): they read fields a historical model does not have.
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

from . import lifecycle

# How long a session survives with no sighting from a runner that posts wholesale
# session reports before it drops out of `state=active`. Such a runner re-reports
# its whole open-task set every `session_report_seconds` (10s) and skips the report
# when it could not read emdash, so "seen within the window" is a direct observation
# of what is open on that box. Three minutes is ~18 report cadences: a runner
# restart never retires anything, and a box that went away stops being offered
# promptly. Derived on every read, so a returning runner un-retires its sessions.
SESSION_LIVE_WINDOW = dt.timedelta(minutes=3)

# A box that posts no wholesale reports (the cloud runner) runs each turn as one
# process that exits when the turn ends, so a finished turn says nothing about
# whether the session is done. A session it holds stays open until it is closed or
# archived or its runner is retired, with three exceptions that retire it once
# nothing on it is in flight and no dialog is up:
#   * it ran on its own (`ran_on_its_own`): nobody is waiting on it;
#   * it is a person's conversation (a web chat, or a Slack thread) started in auto
#     mode, and no turn on it was created or finished within CLOUD_CHAT_IDLE.
# A manual-mode session waits for a person and keeps the open-until-closed rule.
CLOUD_CHAT_IDLE = dt.timedelta(hours=24)

# How long a transferred session stays listed while its new box has not reported it
# yet. The transfer clears the source's identity off the binding and the transfer
# turn is enqueued only once the source packs its work (`handoff.CAPTURE_WAIT`), so
# the last sighting is the source's. Bounded, so a transfer the target never picks
# up still retires like any other quiet session.
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


def _driving_q(session_pk, agent_id, session_key) -> Q:
    """The turns that drive one session: a chat send names the session
    (`chat_session`), a turn the runner opened a session for names it too
    (`Turn.canopy_session`, written by record-session), and an agent turn carries
    the binding's key (`Turn.session_key`). Takes values or `OuterRef`s, so the
    queryset form and the one-session form are the same predicate."""
    q = Q(chat_session=session_pk) | Q(canopy_session=session_pk)
    if agent_id is not None and session_key != "":
        q |= Q(agent=agent_id, session_key=session_key) & ~Q(session_key="")
    return q


def driving_turns():
    """The turns that drive each session, for use under a Session queryset
    (`_driving_q` against the outer row)."""
    Turn = apps.get_model("harness", "Turn")
    return Turn.objects.filter(
        _driving_q(OuterRef("pk"), OuterRef("agent"), OuterRef("runner_binding__session_key")))


def driving_turns_of(session):
    """`driving_turns` for one session in hand."""
    Turn = apps.get_model("harness", "Turn")
    binding = getattr(session, "runner_binding", None)
    key = binding.session_key if binding is not None else ""
    return Turn.objects.filter(_driving_q(session.pk, session.agent_id, key))


def ran_on_its_own(*, mode: str | None, origin: str | None, slack_thread: bool,
                   started_by_agent: bool) -> bool:
    """Nobody is waiting on this session: its first claimed turn ran in `auto` mode,
    and it is not a person's conversation (a web chat or a Slack thread) — or an
    agent started it, whatever channel it came in on. `ran_on_its_own_q` is the
    same rule in SQL; the supervisor feed holds such a session back (`feed.AUTO`)
    and the cloud runner's sessions retire on it (`unseen_q`)."""
    Turn = apps.get_model("harness", "Turn")
    if mode != "auto":
        return False
    conversation = origin == Turn.ORIGIN_CANOPY_WEB_CHAT or slack_thread
    return not conversation or started_by_agent


def started_by_agent_q() -> Q:
    """For a Turn queryset: its initiator is an agent, or an agent's own login (an
    agent calling the API as itself records `kind="user"`)."""
    Agent = apps.get_model("agents", "Agent")
    return Q(initiator_kind="agent") | Q(Exists(Agent.objects.filter(user=OuterRef("initiator_user"))))


def turn_started_by_agent(turn) -> bool:
    """`started_by_agent_q` for one turn in hand."""
    Agent = apps.get_model("agents", "Agent")
    if turn.initiator_kind == "agent":
        return True
    return bool(turn.initiator_user_id) and Agent.objects.filter(user_id=turn.initiator_user_id).exists()


def first_driving_turn():
    """The first claimed turn that drove each session (claimed: an unclaimed turn
    has no mode yet), annotated `_by_agent` — what the session's mode, origin and
    `ran_on_its_own` read."""
    return (driving_turns().exclude(turn_mode="").order_by("created_at")
            .annotate(_by_agent=ExpressionWrapper(started_by_agent_q(), output_field=BooleanField())))


def ran_on_its_own_q() -> Q:
    """`ran_on_its_own` under a Session queryset."""
    Turn = apps.get_model("harness", "Turn")
    first = first_driving_turn()
    mode = Coalesce(Subquery(first.values("turn_mode")[:1]), Value(""))
    origin = Coalesce(Subquery(first.values("origin")[:1]), Value(""))
    by_agent = Coalesce(Subquery(first.values("_by_agent")[:1]), Value(False))
    conversation = Q(Exact(origin, Turn.ORIGIN_CANOPY_WEB_CHAT)) | Q(metadata__has_key="slack_thread")
    return Q(Exact(mode, "auto")) & (~conversation | Q(Exact(by_agent, True)))


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

    # A runner-discovered session could only have come from a report, so absence —
    # of a sighting or of a binding entirely — is staleness with no further
    # qualification.
    #
    # Except on the cloud runner, which records a session per turn and never reports
    # it again: there the rule is `cloud_done` (the note above CLOUD_CHAT_IDLE). Gated
    # on the runner's KIND, not on its never having reported: a laptop that never
    # stamped `sessions_reported_at` is retired by the quiet rule. A session with no
    # binding, or whose binding lost its runner, keeps the rule above.
    unobserved = Q(runner_binding__runner__kind="cloud") & Q(
        runner_binding__runner__sessions_reported_at__isnull=True
    )
    Runner = apps.get_model("harness", "Runner")
    cloud_gone = Q(runner_binding__runner__status=Runner.RETIRED)
    # A cloud-held session that ran on its own is done once nothing on it is in
    # flight. Its mode is the FIRST claimed turn's — the same turn `with_driving_turn`
    # badges — and a later turn still queued or running keeps it open, as does a
    # dialog that is up.
    Turn = apps.get_model("harness", "Turn")
    driving = first_driving_turn()
    # Coalesced: a session with no claimed turn yet must compare FALSE, not NULL —
    # `state=active` EXCLUDES this predicate, and NOT(NULL) would drop the row.
    def first(field):
        return Coalesce(Subquery(driving.values(field)[:1]), Value(""))

    auto = Q(Exact(first("turn_mode"), "auto"))
    # A conversation: a person's web chat, or a session bound to a Slack thread.
    slack_thread = Q(metadata__has_key="slack_thread")
    conversation = Q(Exact(first("origin"), Turn.ORIGIN_CANOPY_WEB_CHAT)) | slack_thread
    ran_on_its_own = ran_on_its_own_q()
    # Idle: no driving turn created or finished within CLOUD_CHAT_IDLE. Turn times,
    # not the binding's: a cloud binding's `live_seen_at` is stamped per turn and
    # its `last_interacted_at` is never reported.
    idle_cutoff = timezone.now() - CLOUD_CHAT_IDLE
    recent = driving_turns().filter(Q(created_at__gte=idle_cutoff) | Q(finished_at__gte=idle_cutoff))
    chat_idle = auto & conversation & ~Q(Exists(recent))
    in_flight = Exists(driving_turns().filter(status__in=Turn.NON_TERMINAL))
    no_dialog = Q(runner_binding__pending_question__isnull=True) | Q(
        runner_binding__pending_question={}
    )
    cloud_finished = (ran_on_its_own | chat_idle) & ~Q(in_flight) & no_dialog
    cloud_done = cloud_gone | cloud_finished

    runner_unseen = Q(origin="runner") & (
        (~unobserved & quiet) | (unobserved & cloud_done)
    )

    # A web chat once a runner holds it: going quiet means the same thing as for a
    # runner-discovered session, but only on a box that posts wholesale reports
    # (`sessions_reported_at` set — see Runner for why it is `isnull` rather than a
    # freshness window). On the cloud runner `live_seen_at` is a per-turn stamp and
    # its age is not evidence, so the cloud rule (`cloud_done`) applies instead.
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
    p.slack_thread = slack_thread
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
    "person_started",     # cloud: a person's web chat or Slack thread with a turn within CLOUD_CHAT_IDLE
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
        When(cloud & (Q(Exact(p.first_origin, Turn.ORIGIN_CANOPY_WEB_CHAT)) | p.slack_thread),
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
    rows = session_model.objects.filter(status="active").filter(unseen_q())
    ids = list(rows.values_list("pk", flat=True))
    changed = session_model.objects.filter(pk__in=ids).update(status="archived")
    if session_model is apps.get_model("canopy_sessions", "Session"):   # a historical one writes no log
        lifecycle.record_many(lifecycle.ARCHIVED, ids, by="system", reason="not seen by any runner",
                              change={"field": "status", "before": "active", "after": "archived"})
    return changed

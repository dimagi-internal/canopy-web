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

from django.db.models import Q
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
    fails the first (no runner yet); a cloud-held chat fails the second. Neither can be
    retired by observation, only by an explicit archive.

    Being wrong in the archiving direction is cheap and self-healing — the rule is
    derived on every read, so one report brings a session back — but it is not free:
    the failure is a live chat vanishing from the list, so the gates above are real.
    """
    # Its binding stopped being reported. `live_seen_at__isnull=True` also covers
    # having no binding at all (the LEFT JOIN yields NULL), which is why the web leg
    # below requires one explicitly.
    quiet = Q(runner_binding__live_seen_at__lt=stale_cutoff()) | Q(
        runner_binding__live_seen_at__isnull=True
    )

    # UNCHANGED. A runner-discovered session could only have come from a report, so
    # absence — of a sighting or of a binding entirely — is staleness with no further
    # qualification. Narrowing this leg would resurrect the 47 zombies of 2026-07-25.
    runner_unseen = Q(origin="runner") & quiet

    # NEW, and gated on an OBSERVER existing. A sent web chat is held and reported
    # like any other, so its going quiet means the same thing — but ONLY on a box that
    # actually posts wholesale reports. The cloud runner does not (it record-sessions
    # once per turn and has no open-task set to report, a cloud chat being always
    # resumable), so there `live_seen_at` is a creation stamp and its age is not
    # evidence. Ungated, this archived every live cloud chat 3 minutes after its last
    # turn. `sessions_reported_at` is that gate; see Runner for why it is `isnull`
    # rather than a freshness window.
    web_unseen = (
        ~Q(origin="runner")
        & Q(runner_binding__isnull=False)
        & Q(runner_binding__runner__sessions_reported_at__isnull=False)
        & quiet
    )

    return runner_unseen | web_unseen


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

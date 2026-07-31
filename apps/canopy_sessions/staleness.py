"""The liveness window, defined once.

Separate from services.py so the backfill migration can import it without dragging
in models, signals, and the realtime bridge. Nothing in here imports app code, so it
is safe for a migration to depend on it long after the rest of the app has moved on.
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

    Keyed on the BINDING, not on origin. Where a chat was started says nothing about
    whether it is still open: once a web chat has been sent, a runner holds an emdash
    session for it and re-reports it every ~10s exactly like a runner-discovered one,
    so absence from that report is the same direct observation for both. Scoping this
    to `origin="runner"` left labs listing 10 week-old phone chats as live whose emdash
    tasks had been deleted days earlier (2026-07-31) — indistinguishable, in the list,
    from a chat you could actually continue.

    Origin survives in one place only: what an ABSENT binding means. A
    runner-discovered session could only ever have come from a report, so having no
    binding is itself staleness; a web chat that has never been sent has no runner yet,
    and absence proves nothing about it, so it stays active until archived by hand.

    An existing binding with no stamp reads as unseen, not fresh: both writers
    (`record_session` and the wholesale report) stamp `live_seen_at` unconditionally,
    so unstamped means never reported. Being wrong here is cheap and self-healing
    anyway — the rule is derived on every read, so one report brings the session back.
    """
    quiet = Q(runner_binding__live_seen_at__lt=stale_cutoff())
    # `live_seen_at__isnull=True` is also true when there is no binding at all (the
    # LEFT JOIN yields NULL), which is why the web leg has to require one explicitly.
    never_reported = Q(runner_binding__live_seen_at__isnull=True) & (
        Q(origin="runner") | Q(runner_binding__isnull=False)
    )
    return quiet | never_reported


def archive_stale_sessions(session_model) -> int:
    """Archive sessions with no recent runner sighting. The one-shot backfill for rows
    that predate any means of retiring them. Only sessions no runner has ever held are
    exempt — see `unseen_q`. Returns the number of rows changed.

    Takes the model class so the migration can pass its historical model and the test
    can pass the real one — the rule itself is identical for both.
    """
    return (
        session_model.objects.filter(status="active")
        .filter(unseen_q())
        .update(status="archived")
    )

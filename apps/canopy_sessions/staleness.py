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
    """Runner-origin sessions with no recent sighting. A session with NO binding at
    all counts as unseen, not as fresh. Web sessions never match — no runner reports
    them, so only an explicit archive ends one."""
    return Q(origin="runner") & (
        Q(runner_binding__live_seen_at__lt=stale_cutoff())
        | Q(runner_binding__live_seen_at__isnull=True)
    )


def archive_stale_sessions(session_model) -> int:
    """Archive runner-origin sessions with no recent runner sighting. The one-shot
    backfill for rows that predate any means of retiring them. Web sessions are exempt
    (no runner reports them). Returns the number of rows changed.

    Takes the model class so the migration can pass its historical model and the test
    can pass the real one — the rule itself is identical for both.
    """
    return (
        session_model.objects.filter(status="active")
        .filter(unseen_q())
        .update(status="archived")
    )

"""The staleness window, defined once.

Separate from services.py so the backfill migration can import it without dragging
in models, signals, and the realtime bridge. Nothing in here imports app code, so it
is safe for a migration to depend on it long after the rest of the app has moved on.
"""
from __future__ import annotations

import datetime as dt

from django.db.models import Q
from django.utils import timezone

# How long a runner-discovered session survives with no runner sighting before it
# drops out of `state=active`. NOT "idle for 3 days": live_seen_at is stamped on
# every reported session each tick, and the runner reports every OPEN emdash task
# regardless of activity — so this measures "fell off the report", i.e. archived,
# deleted, truncated, or the runner was down. An open-but-idle task never expires.
SESSION_STALE_AFTER = dt.timedelta(days=3)


def stale_cutoff(now=None):
    """The live_seen_at floor for `state=active`. A binding last seen before this is
    treated as archived — derived, never written, so it un-archives itself the moment
    the task is reported again."""
    return (now or timezone.now()) - SESSION_STALE_AFTER


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

"""Ask each box to re-ship the transcripts of recent sessions, so their token usage
reaches canopy-web.

    manage.py request_usage_backfill [--days 90] [--dry-run]

History streamed before runners sent usage holds the rows but not the tokens. The
box still has each transcript, and the ordinary backfill (`services.request_backfill`,
which both runners drain from their poll loop) re-ships it with usage on every
assistant record; `persist_transcript_rows` adds the usage to rows it already
holds and credits it (`usage.record_for_chat`). Asking again is harmless: a row
that has its usage is left alone.

Only sessions whose box runs current code are asked (`topology.code_current`): a
backfill is answered once, and a box on older code would answer it without usage
and clear the ask. Re-run it as boxes update; it reports who it is waiting for.
"""
from __future__ import annotations

import datetime as dt
from collections import Counter

from django.core.management.base import BaseCommand
from django.utils import timezone


class Command(BaseCommand):
    help = "Request a transcript backfill for every recent bound session, to recover its token usage."

    def add_arguments(self, parser):
        parser.add_argument("--days", type=int, default=90)
        parser.add_argument("--dry-run", action="store_true")

    def handle(self, *args, days: int, dry_run: bool, **opts):
        from apps.canopy_sessions import services
        from apps.canopy_sessions.models import Session
        from apps.harness.topology import code_current

        since = timezone.now() - dt.timedelta(days=days)
        sessions = (services.with_activity(Session.objects.filter(runner_binding__isnull=False))
                    .filter(_activity__gte=since).select_related("runner_binding__runner"))
        outcome: Counter = Counter()
        waiting: Counter = Counter()
        for s in sessions.iterator(chunk_size=200):
            runner = s.runner_binding.runner
            if runner is not None and code_current(runner) is not True:
                waiting[runner.name] += 1
                continue
            outcome["would request" if dry_run else services.request_backfill(s)] += 1
        for k, n in sorted(outcome.items()):
            self.stdout.write(f"{k}: {n}")
        for name, n in sorted(waiting.items()):
            self.stdout.write(f"waiting for {name} to run current code: {n}")

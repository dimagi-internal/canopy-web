"""`manage.py brain_backfill`: make a `BrainSource` (and its participants) for every session.

Idempotent and resumable (`--after <uuid>`), in batches, and it reads only session rows, never
their messages. Sources are also made on first ask, so this only warms the table."""
from __future__ import annotations

from django.core.management.base import BaseCommand

from apps.brain import sources
from apps.canopy_sessions.models import Session


class Command(BaseCommand):
    help = "Create or refresh the brain's source and participant rows for every session."

    def add_arguments(self, parser):
        parser.add_argument("--batch", type=int, default=500)
        parser.add_argument("--after", default="", help="resume after this session id")

    def handle(self, *args, batch, after, **opts):
        qs = Session.objects.order_by("pk").select_related("runner_binding__runner")
        if after:
            qs = qs.filter(pk__gt=after)
        done = 0
        while True:
            rows = list(qs[:batch])
            if not rows:
                break
            for s in rows:
                sources.for_session(s)
            done += len(rows)
            qs = Session.objects.order_by("pk").select_related("runner_binding__runner").filter(pk__gt=rows[-1].pk)
            self.stdout.write(f"{done} sessions, last {rows[-1].pk}")
        self.stdout.write(self.style.SUCCESS(f"{done} sessions"))

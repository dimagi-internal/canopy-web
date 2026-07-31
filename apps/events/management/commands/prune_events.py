"""Drop old event rows.

A management command rather than a scheduler on purpose — the same reasoning
that kept scheduled turns runner-fired: no celery, no beat, no new deploy
surface. Coalescing already keeps the row count near-flat, so this is
housekeeping, not a load-bearing part of the design.
"""
from __future__ import annotations

import datetime as dt

from django.core.management.base import BaseCommand

from apps.events import services


class Command(BaseCommand):
    help = "Delete events untouched for longer than --older-than-days (default 30)."

    def add_arguments(self, parser) -> None:
        parser.add_argument("--older-than-days", type=int, default=30)
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Report what would be deleted without deleting it.",
        )

    def handle(self, *args, **opts) -> None:
        days = max(1, opts["older_than_days"])
        window = dt.timedelta(days=days)
        if opts["dry_run"]:
            from django.utils import timezone

            from apps.events.models import Event

            n = Event.objects.filter(last_seen_at__lt=timezone.now() - window).count()
            self.stdout.write(f"would delete {n} event(s) untouched for {days}d")
            return
        n = services.prune(older_than=window)
        self.stdout.write(self.style.SUCCESS(f"deleted {n} event(s) untouched for {days}d"))

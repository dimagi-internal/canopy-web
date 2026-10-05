"""Delete attachments that never got claimed by a send.

A management command, not a scheduler — the same reasoning as `prune_events`:
no celery, no beat, no new deploy surface for what is ops housekeeping. An
unbound attachment is rare (the composer blocks sending while an upload is
still in flight) and cheap to accumulate without ever mattering — but a row
with no cleanup path is still a bug, so this gives ops one.
"""
from __future__ import annotations

import datetime as dt

from django.core.management.base import BaseCommand

from apps.canopy_sessions import services


class Command(BaseCommand):
    help = "Delete unsent attachments older than --older-than-hours (default 24)."

    def add_arguments(self, parser) -> None:
        parser.add_argument("--older-than-hours", type=int, default=24)
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Report what would be deleted without deleting it.",
        )

    def handle(self, *args, **opts) -> None:
        hours = max(1, opts["older_than_hours"])
        window = dt.timedelta(hours=hours)
        if opts["dry_run"]:
            from django.utils import timezone

            from apps.canopy_sessions.models import Attachment

            n = Attachment.objects.filter(
                sent_at__isnull=True, created_at__lt=timezone.now() - window,
            ).count()
            self.stdout.write(f"would delete {n} orphaned attachment(s) older than {hours}h")
            return
        n = services.sweep_orphaned_attachments(older_than=window)
        self.stdout.write(self.style.SUCCESS(f"deleted {n} orphaned attachment(s) older than {hours}h"))

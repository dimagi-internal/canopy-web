"""Advance the approved Slack channel ingests that are due, once.

The web process does this on its own clock (apps/slack/ingest_scheduler.py);
this is the same `ingest.tick` for a dev box, or a deployment where that clock
is off (`SLACK_INGEST_TICK_S=0`). It reads only ingests someone approved.

    python manage.py slack_ingest_tick
    python manage.py slack_ingest_tick --limit 20
"""
from __future__ import annotations

from django.core.management.base import BaseCommand

from apps.slack import ingest


class Command(BaseCommand):
    help = "Advance approved Slack channel ingests that are due."

    def add_arguments(self, parser) -> None:
        parser.add_argument("--limit", type=int, default=5, help="Most ingests to advance")

    def handle(self, *args, **opts) -> None:
        n = ingest.tick(limit=opts["limit"])
        self.stdout.write(f"advanced {n} ingest(s)")

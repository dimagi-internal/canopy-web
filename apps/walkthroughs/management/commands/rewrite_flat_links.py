"""Rewrite flat artifact links stored in canopy's data to `/w/<workspace>/…`.

The `walkthroughs/0010` data migration runs this once on deploy; this command is
for counting first (`--dry-run`), for finishing the HTML decks if the migration
ran out of its time budget, and for any row that slipped in with a flat link
since (a client that still prints one). Idempotent. See
`apps/walkthroughs/flat_links.py`.
"""
from __future__ import annotations

from django.apps import apps
from django.core.management.base import BaseCommand

from apps.walkthroughs import flat_links


class Command(BaseCommand):
    help = "Rewrite stored flat /walkthrough|/review|/share|/storyboard|/narrative|/ddd-release links to /w/<ws>/…"

    def add_arguments(self, parser):
        parser.add_argument("--dry-run", action="store_true", help="Count what would change; write nothing.")
        parser.add_argument("--no-blobs", action="store_true", help="Skip the HTML decks stored in Drive.")
        parser.add_argument("--budget", type=float, default=1800.0,
                            help="Seconds to spend on Drive blobs before stopping (default 1800).")

    def handle(self, *args, dry_run=False, no_blobs=False, budget=1800.0, **options):
        stats = flat_links.sweep(apps, dry_run=dry_run, blobs=not no_blobs, budget_s=budget)
        self.stdout.write(flat_links.summary(stats, dry_run=dry_run))

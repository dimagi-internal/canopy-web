"""Show (default) or apply what content retention would drop.

    manage.py retention_sweep                    # dry run: counts per rule, writes nothing
    manage.py retention_sweep --apply            # purge now, up to one run's batch limits
    manage.py retention_sweep --apply --no-limit # purge the whole backlog

`--apply` works whether or not CANOPY_RETENTION_ENFORCE is on: running it is
the deliberate act. Enforcement only governs the automatic heartbeat sweep.
"""
from __future__ import annotations

import json

from django.conf import settings
from django.core.management.base import BaseCommand

from apps.retention import services
from apps.retention.models import RetentionRule


class Command(BaseCommand):
    help = "Dry-run (default) or apply the content retention rules."

    def add_arguments(self, parser):
        parser.add_argument("--apply", action="store_true", help="Delete, rather than only count.")
        parser.add_argument("--no-limit", action="store_true",
                            help="Ignore the per-run batch limits (clear the whole backlog).")

    def handle(self, *args, apply: bool, no_limit: bool, **options):
        rules = RetentionRule.objects.count()
        enforce = getattr(settings, "CANOPY_RETENTION_ENFORCE", False)
        self.stdout.write(f"rules: {rules}   automatic enforcement: {'ON' if enforce else 'off'}")
        if not rules:
            self.stdout.write("No retention rules, so nothing is ever purged. Add some in /admin/retention/.")
        limits = {k: None for k in services.BATCH_LIMITS} if no_limit else None
        record = services.sweep(apply=apply, limits=limits)
        verb = "PURGED" if apply else "would purge (dry run)"
        self.stdout.write(f"{verb}:")
        self.stdout.write(json.dumps(record.counts, indent=2, sort_keys=True))
        if not apply and not no_limit:
            self.stdout.write(f"(counts are capped at one run's batch limits: {services.BATCH_LIMITS}; "
                              "add --no-limit to see the whole backlog)")

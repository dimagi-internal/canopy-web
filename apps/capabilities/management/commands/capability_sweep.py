"""Run the capability sweep once, now, and print what it did.

The sweep normally runs on its own (apps/capabilities/scheduler.py); this is for
an operator in an ECS Exec shell. It bypasses the fleet-wide dedupe key.
"""
from __future__ import annotations

import json

from django.core.management.base import BaseCommand


class Command(BaseCommand):
    help = "Run the cloud-capability sweep once (expiry, idle release, queue, orphan kill)."

    def handle(self, *args, **options):
        from apps.capabilities import services

        self.stdout.write(json.dumps(services.sweep(), sort_keys=True))

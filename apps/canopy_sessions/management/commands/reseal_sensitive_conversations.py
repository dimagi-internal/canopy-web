"""Move sensitive conversations still sealed under the field key to their own keys.

Migration 0049 did this for every conversation whose starter had a vault key. One
whose starter had none was left under the field key (and is not served to anyone
meanwhile); run this once they have set one up. The re-seal itself is
`conversation_keys.reseal_legacy`, the same function the migration ran.
"""
from __future__ import annotations

from django.core.management.base import BaseCommand

from apps.canopy_sessions import conversation_keys


class Command(BaseCommand):
    help = "Re-seal sensitive conversations still under the field key to their own conversation keys."

    def handle(self, *args, **options):
        done = conversation_keys.reseal_legacy()
        self.stdout.write(f"re-sealed {done['rows']} row(s) in {done['sessions']} conversation(s)")
        for session_id in done["pending"]:
            self.stdout.write(f"still needs migration (its starter has no vault key): {session_id}")

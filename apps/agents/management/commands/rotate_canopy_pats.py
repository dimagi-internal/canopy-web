"""Run one canopy-pat rotation tick by hand (Canopy Vault Phase 1b, canopy-web#1549).

The web process does this on its own clock (apps/agents/pat_rotation_scheduler.py);
this is for an operator: rotate every agent that is due, revoke every old token
whose 24 h overlap has ended, and print the counts. `--dry-run` lists what is due.
"""
from __future__ import annotations

from django.core.management.base import BaseCommand

from apps.agents.credentials import pat_rotation


class Command(BaseCommand):
    help = "Rotate due agent canopy-pats and revoke old tokens past their overlap."

    def add_arguments(self, parser):
        parser.add_argument("--dry-run", action="store_true", help="List due agents; change nothing.")

    def handle(self, *args, dry_run: bool = False, **options):
        due = pat_rotation.due_agents()
        if dry_run:
            for agent in due:
                self.stdout.write(f"due: {agent.slug} (every {agent.pat_rotation_days}d)")
            self.stdout.write(f"{len(due)} agent(s) due")
            return
        out = pat_rotation.tick()
        self.stdout.write(f"rotated {out['rotated']}, failed {out['failed']}, revoked {out['revoked']}")

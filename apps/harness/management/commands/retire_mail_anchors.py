"""Retire every mail-thread anchor session nothing is live on (apps/harness/mail_anchor.py).

The periodic tick does this on its own; this is the by-hand run, and with
--dry-run the list of what it would retire and why the rest stay open.
"""
from django.core.management.base import BaseCommand

from apps.harness import mail_anchor


class Command(BaseCommand):
    help = "Retire mail-thread anchor sessions whose turns and work sessions are all finished."

    def add_arguments(self, parser):
        parser.add_argument("--dry-run", action="store_true",
                            help="List what would retire, and why the rest stay open; change nothing.")

    def handle(self, *args, dry_run=False, **options):
        if dry_run:
            for s in mail_anchor.candidates():
                reason = mail_anchor.why_not(s)
                self.stdout.write(f"{s.pk}  {'RETIRE' if not reason else 'keep: ' + reason}  {s.title}")
        done = mail_anchor.sweep(dry_run=dry_run)
        verb = "would retire" if dry_run else "retired"
        self.stdout.write(self.style.SUCCESS(f"{verb} {len(done)} mail anchor session(s)"))

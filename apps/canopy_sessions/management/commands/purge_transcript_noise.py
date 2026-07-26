"""Remove harness records that were persisted as user messages.

`persist_transcript_rows` now drops these on the way in, but rows written before
that fix are still in the table — and they are what a person actually sees when
they open an affected chat. This clears the backlog.

DRY RUN BY DEFAULT. It deletes rows, so it asks to be told twice: run it bare to
see the count and a sample, then `--apply` to commit. Deliberately a command and
not a data migration — deleting is irreversible, and "how much of my chat history
does this touch" is a question worth answering before, not after.
"""
from __future__ import annotations

from django.core.management.base import BaseCommand
from django.db.models import Q

from apps.canopy_sessions.models import Message
from apps.canopy_sessions.transcript_noise import SYSTEM_NOISE_PREFIXES


class Command(BaseCommand):
    help = "Delete harness records (task notifications, system reminders) stored as user messages."

    def add_arguments(self, parser):
        parser.add_argument(
            "--apply", action="store_true",
            help="Actually delete. Without it, only report what would go.",
        )
        parser.add_argument(
            "--sample", type=int, default=5,
            help="How many matches to print (default 5).",
        )

    def handle(self, *args, **options):
        # istartswith mirrors is_system_noise: prefix-anchored so a human quoting
        # a marker keeps their message. It cannot mirror the .lstrip(), so a row
        # with leading whitespace survives here — acceptable for a cleanup pass
        # (the ingest filter is the authority; this is only catching up history).
        predicate = Q()
        for prefix in SYSTEM_NOISE_PREFIXES:
            predicate |= Q(plaintext__istartswith=prefix)
        qs = Message.objects.filter(predicate, role=Message.USER)

        total = qs.count()
        if not total:
            self.stdout.write(self.style.SUCCESS("No harness records found."))
            return

        sessions = qs.values("session_id").distinct().count()
        self.stdout.write(f"{total} harness record(s) across {sessions} session(s).")
        for row in qs.order_by("-created_at")[: options["sample"]]:
            head = " ".join((row.plaintext or "").split())[:90]
            self.stdout.write(f"  session={row.session_id} index={row.turn_index} :: {head}")

        if not options["apply"]:
            self.stdout.write(self.style.WARNING("Dry run — re-run with --apply to delete."))
            return

        deleted, _ = qs.delete()
        self.stdout.write(self.style.SUCCESS(f"Deleted {deleted} row(s)."))

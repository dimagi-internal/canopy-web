"""Print how every agent answers (see `apps/agents/answering_snapshot.py`). Reads only."""
from django.core.management.base import BaseCommand, CommandError

from apps.agents import answering_snapshot


class Command(BaseCommand):
    help = ("How every agent answers: profiles, the decision matrix, site listings, statements, A2A cards "
            "and topology. Reads only; the same data prints the same bytes. Without --json, prints its hash.")

    def add_arguments(self, parser):
        parser.add_argument("--json", action="store_true", help="Print the whole document.")

    def handle(self, *args, **opts):
        try:
            doc = answering_snapshot.take()
        except answering_snapshot.SnapshotError as exc:
            raise CommandError(str(exc)) from exc
        if opts["json"]:
            self.stdout.write(answering_snapshot.as_json(doc))
        else:
            self.stdout.write(f"{answering_snapshot.digest(doc)}  agents={len(doc['agents'])} "
                              f"sites={len(doc['sites'])}")

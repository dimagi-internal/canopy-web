"""List the agents that read mode would newly let people into (see `apps/agents/read_mode_gate.py`)."""
import json

from django.core.management.base import BaseCommand

from apps.agents import read_mode_gate


class Command(BaseCommand):
    help = ("Agents with a read-mode line (own or workspace default) and no read mode of their own: "
            "inert before read mode resolved on its own, live after. Exit 1 when there are any.")

    def add_arguments(self, parser):
        parser.add_argument("--json", action="store_true")

    def handle(self, *args, **opts):
        hits = read_mode_gate.exposures()
        if opts["json"]:
            self.stdout.write(json.dumps(hits, indent=2))
        elif not hits:
            self.stdout.write("none: no agent has a read-mode line and no read mode of its own")
        else:
            for h in hits:
                self.stdout.write(f"{h['workspace']}/{h['agent']}")
                for ln in h["lines"]:
                    self.stdout.write(f"    {ln}")
        if hits:
            raise SystemExit(1)

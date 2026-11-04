"""List who would lose the right to change an agent when its routes gate on `agent_edit`."""
import json

from django.core.management.base import BaseCommand

from apps.agents import agent_edit_check


class Command(BaseCommand):
    help = ("Every (agent, person) who may change an agent today (workspace editor or above, or an "
            "agent admin) and may not once the routes gate on `agent_edit`. Read-only. Exit 1 when any.")

    def add_arguments(self, parser):
        parser.add_argument("--json", action="store_true")
        parser.add_argument("--all", action="store_true", help="every row, not only the losses")

    def handle(self, *args, **opts):
        rows = agent_edit_check.edit_rights() if opts["all"] else agent_edit_check.losses()
        if opts["json"]:
            self.stdout.write(json.dumps(rows, indent=2))
        elif not rows:
            self.stdout.write("none: everyone who may change an agent today still may")
        else:
            for r in rows:
                self.stdout.write(f"{r['workspace']}/{r['agent']}\t{r['email']}\t{r['workspace_role']}\t"
                                  f"{'keeps' if r['after'] else 'LOSES'}\t{r['agent_role'] or '-'}\t{r['via'] or '-'}")
        if not opts["all"] and rows:
            raise SystemExit(1)

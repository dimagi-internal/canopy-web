"""Check that every access-list line grants what it granted before roles became rows."""
import json

from django.core.management.base import BaseCommand

from apps.agents import role_parity


class Command(BaseCommand):
    help = ("Compare what every access-list line grants now (its role's row) with what the code table "
            "gave it. Before the migration: every role string must be a built-in. After: every line "
            "linked to its own tree's row with the same permissions. Exit 1 when anything needs a person.")

    def add_arguments(self, parser):
        parser.add_argument("--json", action="store_true")

    def handle(self, *args, **opts):
        report = role_parity.check()
        if opts["json"]:
            self.stdout.write(json.dumps(report, indent=2, default=str))
        else:
            self.stdout.write(f"{report['lines']} role lines in {len(report['trees'])} trees "
                              f"({'after' if report['migrated'] else 'BEFORE'} the roles migration)")
            for name in ("unknown", "unlinked", "wrong_tree", "mismatched", "edited"):
                for entry in report[name]:
                    self.stdout.write(f"  {name}: {entry}")
            self.stdout.write("ok: every line grants what it granted" if report["ok"] else "NOT OK")
        if not report["ok"]:
            raise SystemExit(1)

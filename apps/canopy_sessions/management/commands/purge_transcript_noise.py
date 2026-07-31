"""Remove harness records that were persisted as user messages.

The ingest filters drop these on the way in; rows written before a given prefix
was recognised are still in the table, and they are what a person actually sees
when they open an affected chat. This clears the backlog.

PREFER THE MCP TOOLS. `audit_session_noise` / `purge_session_noise` do the same
work over HTTPS, scoped to the caller's own workspaces, rate-limited and audited
— no AWS credentials, no one-off container. This command exists for the case MCP
deliberately cannot serve: an unscoped sweep across EVERY workspace, which is a
privileged operation and stays behind shell access on purpose.

DRY RUN BY DEFAULT. It deletes rows, so it asks to be told twice: run it bare to
see the count and a sample, then `--apply` to commit. Deliberately a command and
not a data migration — deleting is irreversible, and "how much of my chat history
does this touch" is a question worth answering before, not after.

The matching and deleting live in `apps.canopy_sessions.maintenance`, shared with
the MCP tools, so this command cannot develop its own idea of what noise is.
"""
from __future__ import annotations

from django.core.management.base import BaseCommand, CommandError

from apps.canopy_sessions import maintenance


class Command(BaseCommand):
    help = "Delete harness records (task notifications, skill bodies, …) stored as user messages."

    def add_arguments(self, parser):
        parser.add_argument(
            "--apply", action="store_true",
            help="Actually delete. Without it, only report what would go.",
        )
        parser.add_argument(
            "--sample", type=int, default=5,
            help="How many matches to print (default 5).",
        )
        parser.add_argument(
            "--session", default=None,
            help="Restrict to one session id (UUID).",
        )
        parser.add_argument(
            "--workspace", action="append", default=None, metavar="SLUG",
            help="Restrict to a workspace slug. Repeatable. Omit to sweep ALL workspaces.",
        )

    def handle(self, *args, **options):
        # Unscoped is the DEFAULT here (it's the reason this command still
        # exists) but never silent: an operator deleting across every tenant in
        # the deployment should see that stated before the numbers.
        slugs = options["workspace"] or maintenance.ALL_WORKSPACES
        if slugs is maintenance.ALL_WORKSPACES:
            self.stdout.write(self.style.WARNING("Scope: ALL workspaces."))
        else:
            self.stdout.write(f"Scope: workspace(s) {', '.join(slugs)}.")

        try:
            report = maintenance.purge_noise(
                workspace_slugs=slugs,
                session_id=options["session"],
                sample=options["sample"],
                apply=options["apply"],
            )
        except (ValueError, TypeError) as exc:
            # Overwhelmingly a malformed --session UUID; surface it as a usage
            # error rather than a traceback.
            raise CommandError(f"Bad argument: {exc}") from exc

        if not report["matched"]:
            self.stdout.write(self.style.SUCCESS("No harness records found."))
            return

        self.stdout.write(
            f"{report['matched']} harness record(s) across {report['sessions']} session(s)."
        )
        for row in report["sample"]:
            self.stdout.write(
                f"  session={row['session_id']} index={row['turn_index']} :: {row['text']}"
            )

        if not report["applied"]:
            self.stdout.write(self.style.WARNING("Dry run — re-run with --apply to delete."))
            return
        self.stdout.write(self.style.SUCCESS(f"Deleted {report['deleted']} row(s)."))

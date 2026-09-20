"""Declare the Slack app an agent, so Slack draws its own working indicator.

A management command rather than a deploy step or a button: it is one-way in
part (`agent_view` cannot be swapped back to `assistant_view`) and it changes
how the app presents itself, so it should be a thing somebody decided to do.

    python manage.py slack_declare_agent --team T0123          # or --workspace
    python manage.py slack_declare_agent --team T0123 --dry-run

Afterwards the app must be RE-INSTALLED once (the new scope only lands on a
fresh OAuth grant); the command prints the URL.
"""
from __future__ import annotations

from django.core.management.base import BaseCommand, CommandError

from apps.slack import commands as slack_commands
from apps.slack.models import SlackInstallation


class Command(BaseCommand):
    help = "Declare the Slack app an agent (native working indicator + Stop button)."

    def add_arguments(self, parser) -> None:
        parser.add_argument("--team", default="", help="Slack team id (T…)")
        parser.add_argument("--workspace", default="", help="canopy workspace slug")
        parser.add_argument("--description", default="", help="agent_view description, max 300 chars")
        parser.add_argument("--dry-run", action="store_true")

    def handle(self, *args, **opts) -> None:
        rows = SlackInstallation.objects.all()
        if opts["team"]:
            rows = rows.filter(team_id=opts["team"])
        if opts["workspace"]:
            rows = rows.filter(workspace_id=opts["workspace"])
        installs = list(rows[:2])
        if not installs:
            raise CommandError("no matching Slack installation")
        if len(installs) > 1:
            raise CommandError("more than one installation matches — name one with --team")
        installation = installs[0]
        if opts["dry_run"]:
            self.stdout.write(f"would declare {installation.team_name or installation.team_id} an agent: "
                              f"features.agent_view, scope {slack_commands.AGENT_SCOPE}, "
                              f"events {', '.join(slack_commands.AGENT_EVENTS)}")
            return
        try:
            result = slack_commands.declare_agent(installation, description=opts["description"])
        except slack_commands.NotConfigured as e:
            raise CommandError(f"{e} — connect the app's configuration token on the Slack page first")
        except Exception as e:  # noqa: BLE001 — a Slack refusal should read as one line, not a traceback
            raise CommandError(str(e))
        if not result["changed"]:
            self.stdout.write(self.style.SUCCESS("already declared an agent — nothing to change"))
            return
        self.stdout.write(self.style.SUCCESS("changed: " + "; ".join(result["changed"])))
        if result["reinstall_required"]:
            from apps.slack.services import public_url

            self.stdout.write(self.style.WARNING(
                "RE-INSTALL the app once for the new scope to take effect: "
                + public_url("/auth/slack/install/")))

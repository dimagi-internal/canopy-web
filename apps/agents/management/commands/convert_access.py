"""Move agents from their published rules to their access list.

    manage.py convert_access                    # dry run, every agent not yet converted
    manage.py convert_access --agent hal        # dry run, one agent
    manage.py convert_access --agent hal --apply

A dry run prints the lines each agent would get and whether `access.decide` answers
exactly as before for everyone who could plausibly ask. `--apply` writes only the agents
that convert cleanly; one that does not is reported and left on its old rules.
See `apps/agents/convert_access.py` and the 2026-10-30 agent-roles spec.
"""
from __future__ import annotations

from django.core.management.base import BaseCommand, CommandError

from apps.agents import convert_access
from apps.agents.models import Agent


class Command(BaseCommand):
    help = "Translate agents' published access rules into access-list lines (dry run unless --apply)."

    def add_arguments(self, parser):
        parser.add_argument("--agent", action="append", default=[], help="an agent slug (repeatable)")
        parser.add_argument("--apply", action="store_true", help="write the lines and switch the agent over")

    def handle(self, *args, agent, apply, **opts):
        agents = Agent.objects.filter(slug__in=agent) if agent else Agent.objects.all()
        missing = set(agent) - set(agents.values_list("slug", flat=True))
        if missing:
            raise CommandError(f"no such agent: {', '.join(sorted(missing))}")
        applied = left = 0
        for a in agents.order_by("slug"):
            conv = convert_access.plan(a)
            if conv.already:
                self.stdout.write(f"{a.slug}: already on its access list")
                continue
            self.stdout.write(f"{a.slug}: {'OK' if conv.ok else 'NOT CLEAN'} ({conv.probed} probes)")
            for line in conv.lines:
                self.stdout.write(f"    {line.describe()}")
            for item in [*conv.unsupported, *conv.differences]:
                self.stdout.write(self.style.WARNING(f"    ! {item}"))
            if apply and conv.ok:
                convert_access.apply(conv)
                applied += 1
            elif apply:
                left += 1
        if apply:
            self.stdout.write(f"applied {applied}, left on their old rules {left}")

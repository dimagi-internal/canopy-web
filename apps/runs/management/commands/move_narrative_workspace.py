"""Move a narrative to another workspace, from the shell.

A thin wrapper over ``apps.runs.transfer`` — the same service the REST route
uses (``POST /api/ddd/narratives/{slug}/move/``), so the two surfaces cannot
drift. Prefer the API when you have a session or PAT; this exists for the case
where you have a shell and no credentials, and for scripted repair.

Unlike the API it does NOT check membership: a shell already implies full
access. That is the one deliberate difference between the surfaces.

DRY RUN BY DEFAULT.

    python manage.py move_narrative_workspace --to connect \
        --slug verified-monitoring --slug microplans-study-groups
    python manage.py move_narrative_workspace --to connect --slug … --apply
"""
from __future__ import annotations

from django.core.management.base import BaseCommand, CommandError

from apps.runs.transfer import apply_move, plan_move
from apps.workspaces.models import Workspace


class Command(BaseCommand):
    help = "Move every artifact of one or more narratives into a target workspace."

    def add_arguments(self, parser):
        parser.add_argument("--to", required=True, help="Target workspace slug.")
        parser.add_argument(
            "--slug", action="append", default=[], required=True,
            help="Narrative slug (repeatable).",
        )
        parser.add_argument(
            "--apply", action="store_true",
            help="Actually move the rows. Without it this only reports.",
        )

    def handle(self, *args, **opts):
        target = opts["to"]
        slugs = set(opts["slug"])
        if not Workspace.objects.filter(slug=target).exists():
            raise CommandError(f"no such workspace: {target}")

        plan = plan_move(slugs, target)

        for slug, info in plan["narratives"].items():
            self.stdout.write(f"{slug}:")
            for ws, versions in info["versions_by_workspace"].items():
                mark = "  (target)" if ws == target else ""
                split = "  SPLIT" if info["split"] else ""
                self.stdout.write(f"    {ws}: v{versions}{mark}{split}")
            self.stdout.write(f"    walkthroughs: {info['walkthroughs']}")

        self.stdout.write(
            f"\nWould move {plan['reviews_to_move']} review(s), "
            f"{plan['walkthroughs_to_move']} walkthrough(s) and "
            f"{plan['storyboards_to_move']} storyboard(s) into {target!r}."
        )

        if not opts["apply"]:
            self.stdout.write(self.style.WARNING("DRY RUN — pass --apply to execute."))
            return

        result = apply_move(slugs, target)
        self.stdout.write(
            self.style.SUCCESS(
                f"Moved {result['reviews_to_move']} review(s), "
                f"{result['walkthroughs_to_move']} walkthrough(s) and "
                f"{result['storyboards_to_move']} storyboard(s)."
            )
        )

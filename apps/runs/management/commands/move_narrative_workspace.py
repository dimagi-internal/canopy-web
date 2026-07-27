"""Move a narrative's artifacts to another workspace.

A DDD narrative is not a table — it is inferred at read time from the
``ReviewRequest`` and ``Walkthrough`` rows that share its slug. So a narrative
"living in" a workspace really means every one of those rows carries that
workspace, and nothing keeps them together: post a version from a differently
scoped caller and the lineage silently splits across tenants.

That is not hypothetical. On labs, ``create-survey-solicitation`` had v12 and
v7..v1 in ``dimagi`` while v8..v11 sat in ``connect`` — so the narrative's own
version history was unreadable from either side, and a storyboard scoped to one
tenant diffed v12 against v7 instead of v11.

DRY RUN BY DEFAULT. Pass ``--apply`` to execute. Mirrors ``audit_auto_join``.

    python manage.py move_narrative_workspace --to connect \
        --slug verified-monitoring --slug microplans-study-groups
    python manage.py move_narrative_workspace --to connect --slug … --apply
"""
from __future__ import annotations

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from apps.reviews.models import ReviewRequest
from apps.runs.aggregate import narrative_of_review, narrative_of_walkthrough
from apps.storyboards.models import Entry, Storyboard
from apps.walkthroughs.models import Walkthrough
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

        reviews = [r for r in ReviewRequest.objects.all() if narrative_of_review(r) in slugs]
        walkthroughs = [
            w for w in Walkthrough.objects.all() if narrative_of_walkthrough(w) in slugs
        ]

        moving_reviews = [r for r in reviews if r.workspace_id != target]
        moving_wts = [w for w in walkthroughs if w.workspace_id != target]

        # A storyboard resolves its entries against ITS OWN workspace, so a board
        # left behind after its narratives move renders nothing but placeholders.
        # Moving the narratives without it would just relocate the split.
        board_ids = set(
            Entry.objects.filter(narrative_slug__in=slugs).values_list(
                "act__storyboard_id", flat=True
            )
        )
        moving_boards = [
            b for b in Storyboard.objects.filter(id__in=board_ids) if b.workspace_id != target
        ]

        for slug in sorted(slugs):
            by_ws: dict[str | None, list[int]] = {}
            for r in reviews:
                if narrative_of_review(r) == slug:
                    by_ws.setdefault(r.workspace_id, []).append(r.version or 0)
            self.stdout.write(f"{slug}:")
            for ws, versions in sorted(by_ws.items(), key=lambda kv: str(kv[0])):
                mark = "  (target)" if ws == target else ""
                self.stdout.write(f"    {ws}: v{sorted(versions)}{mark}")
            n_w = sum(1 for w in walkthroughs if narrative_of_walkthrough(w) == slug)
            self.stdout.write(f"    walkthroughs: {n_w}")

        for b in moving_boards:
            self.stdout.write(f"storyboard {b.slug!r}: {b.workspace_id} -> {target}")

        self.stdout.write(
            f"\nWould move {len(moving_reviews)} review(s), {len(moving_wts)} "
            f"walkthrough(s) and {len(moving_boards)} storyboard(s) into {target!r}."
        )

        if not opts["apply"]:
            self.stdout.write(self.style.WARNING("DRY RUN — pass --apply to execute."))
            return

        with transaction.atomic():
            for r in moving_reviews:
                r.workspace_id = target
                r.save(update_fields=["workspace"])
            for w in moving_wts:
                w.workspace_id = target
                w.save(update_fields=["workspace"])
            for b in moving_boards:
                b.workspace_id = target
                b.save(update_fields=["workspace", "updated_at"])

        self.stdout.write(
            self.style.SUCCESS(
                f"Moved {len(moving_reviews)} review(s), {len(moving_wts)} walkthrough(s) "
                f"and {len(moving_boards)} storyboard(s)."
            )
        )

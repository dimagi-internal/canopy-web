"""Link EXISTING artifacts to their agent project — only where the link is unambiguous.

Board task hal/T76. New uploads are stamped at create (apps/harness/artifact_origin.py);
this fills in what was made before that, from the one record that already names a
project for DDD work: the run doc (``AgentRun``, kind ddd — ``ext_id`` = the run
id, ``subject`` = the narrative, ``project`` = the agent project).

For each walkthrough / narrative review / storyboard with no ``agent_project``:

1. **its run id is a run doc's ext_id** → that run doc's project (exact).
2. else **its narrative is the subject of run docs that ALL name ONE project** →
   that project. Two projects claiming a narrative is ambiguous: skipped.
3. a storyboard → the one project every narrative on it resolves to; skipped
   when they disagree or any is unresolved.

A project is only ever written into an artifact in the SAME workspace as the
project's agent. Nothing else is guessed: the session/turn an old artifact came
from was never recorded anywhere, so it is not backfilled; neither is the repo
``project_slug`` (no record names it).

Dry run by default — prints what it would link; ``--apply`` writes.
"""
from __future__ import annotations

from collections import defaultdict

from django.core.management.base import BaseCommand

from apps.agent_runs.models import AgentRun
from apps.reviews.models import ReviewRequest
from apps.runs.ddd import narrative_slug_from_run_id
from apps.storyboards.models import Storyboard
from apps.walkthroughs.models import Walkthrough


class Command(BaseCommand):
    help = "Link existing artifacts to their agent project where a run doc makes it unambiguous."

    def add_arguments(self, parser):
        parser.add_argument("--apply", action="store_true", help="Write the links (default: dry run).")

    def handle(self, *args, apply: bool = False, **opts):
        runs = list(
            AgentRun.objects.filter(project__isnull=False)
            .select_related("project__agent")
            .only("ext_id", "subject", "project__id", "project__agent__workspace_id")
        )
        by_run = {r.ext_id: r.project for r in runs if r.ext_id}
        by_subject: dict[str, dict] = defaultdict(dict)
        for r in runs:
            if r.subject:
                by_subject[r.subject][r.project.pk] = r.project

        def resolve(run_id: str | None, slug: str | None):
            if run_id and run_id in by_run:
                return by_run[run_id], "run"
            slug = slug or (narrative_slug_from_run_id(run_id) if run_id else None)
            claims = by_subject.get(slug or "", {})
            if len(claims) == 1:
                return next(iter(claims.values())), "subject"
            return None, ("ambiguous" if len(claims) > 1 else "unresolved")

        stats: dict[str, int] = defaultdict(int)

        def link(obj, project, how: str, label: str) -> None:
            if project is None:
                stats[f"{label}:{how}"] += 1
                return
            if project.agent.workspace_id != obj.workspace_id:
                stats[f"{label}:other-workspace"] += 1
                return
            stats[f"{label}:linked-by-{how}"] += 1
            self.stdout.write(f"{label} {obj.pk} -> {project.agent.slug}/{project.ext_id} ({how})")
            if apply:
                type(obj).objects.filter(pk=obj.pk, agent_project__isnull=True).update(
                    agent_project=project
                )

        for w in Walkthrough.objects.filter(agent_project__isnull=True).exclude(run_id__isnull=True).exclude(run_id=""):
            project, how = resolve(w.run_id, w.narrative_slug)
            link(w, project, how, "walkthrough")

        for r in ReviewRequest.objects.filter(agent_project__isnull=True):
            project, how = resolve(r.run_id or None, r.narrative_slug)
            link(r, project, how, "review")

        for b in Storyboard.objects.filter(agent_project__isnull=True).prefetch_related("acts__entries"):
            slugs = {e.narrative_slug for a in b.acts.all() for e in a.entries.all()}
            found = {resolve(None, s)[0] for s in slugs} if slugs else {None}
            if None in found or len(found) != 1:
                stats["storyboard:" + ("ambiguous" if len(found - {None}) > 1 else "unresolved")] += 1
                continue
            link(b, found.pop(), "narratives", "storyboard")

        verb = "linked" if apply else "would link (dry run — pass --apply)"
        self.stdout.write(f"{verb}: " + ", ".join(f"{k}={v}" for k, v in sorted(stats.items())))

"""`manage.py brain_validate_links <handle> --workspace <slug> --truth truth.json`: how well a project's
stored links match a hand-checked list of session ids (a JSON list). Prints precision and recall of
the `core` tier (`--tiers core adjacent` to widen) and the sessions each side missed.

`--seed` first widens the project's signature from its certain sessions (`associate.seed_signature`),
and `--rerun` looks at every session of the workspace again, so a changed signature is measured."""
from __future__ import annotations

import json

from django.core.management.base import BaseCommand, CommandError

from apps.agents.models import AgentProject
from apps.brain import associate, sources
from apps.canopy_sessions.models import Session


class Command(BaseCommand):
    help = "Measure a project's session links against a hand-checked list."

    def add_arguments(self, parser):
        parser.add_argument("handle")
        parser.add_argument("--workspace", required=True)
        parser.add_argument("--truth", required=True, help="a JSON file: a list of session ids")
        parser.add_argument("--tiers", nargs="+", default=["core"])
        parser.add_argument("--how", default="", help="only links made this way: dispatched, signals, judged, declared")
        parser.add_argument("--seed", action="store_true")
        parser.add_argument("--rerun", action="store_true")

    def handle(self, *args, handle, workspace, truth, tiers, how, seed, rerun, **opts):
        project = AgentProject.objects.filter(workspace_id=workspace, handle=handle).first()
        if project is None:
            raise CommandError(f"no project {handle!r} in {workspace!r}")
        with open(truth) as f:
            ids = json.load(f)
        if seed:
            associate.seed_signature(project)
        if rerun:
            for s in Session.objects.filter(workspace_id=workspace).iterator():
                associate.associate(sources.for_session(s))
        self.stdout.write(json.dumps(associate.compare(project, ids, tiers=tiers, how=how), indent=2))

"""Backfill Session.activity from the Message rows the server already holds.

Sessions ingested before activity was folded at ingest have an empty
`activity`. Everything derivable from stored rows — PRs created / merge-requested
(from `gh pr create|merge` calls and their output), remotes, branches a command
created or a push printed, repos from `cd` / `git -C` / edited paths, edited
directories, MCP tool counts — is recomputed here.

What it CANNOT recover is the per-record `cwd` / `gitBranch`: those ride beside a
row on the wire and are never stored, so for an old session they arrive only when
a current runner re-ships its transcript (a reset, a backfill, a re-attach).
Retention-purged rows are gone, so a purged session rebuilds from what is left.

Idempotent and non-destructive — it only writes `activity`, and keeps any
context a session already has. Safe to re-run.

    manage.py rebuild_session_activity                 # sessions with empty activity
    manage.py rebuild_session_activity --all           # every session with rows
    manage.py rebuild_session_activity --session <id>  # one (repeatable)
"""
from __future__ import annotations

from django.core.management.base import BaseCommand
from django.db import transaction

from apps.canopy_sessions import activity
from apps.canopy_sessions.models import Session


class Command(BaseCommand):
    help = "Recompute Session.activity from stored transcript rows."

    def add_arguments(self, parser):
        parser.add_argument("--all", action="store_true",
                            help="Rebuild every session with rows, not only empty ones.")
        parser.add_argument("--session", action="append", default=[],
                            help="Rebuild only this session id (repeatable).")
        parser.add_argument("--dry-run", action="store_true",
                            help="Compute and report, write nothing.")

    def handle(self, *args, **opts):
        qs = Session.objects.filter(messages__isnull=False).distinct()
        if opts["session"]:
            qs = Session.objects.filter(pk__in=opts["session"])
        elif not opts["all"]:
            qs = qs.filter(activity={})
        seen = changed = 0
        # Materialised, not .iterator(): each session commits its own transaction,
        # which would invalidate a server-side cursor held across the loop.
        for pk in list(qs.values_list("pk", flat=True)):
            # Locked like the ingest path, so a ship landing mid-rebuild is
            # folded after it rather than overwritten by it.
            with transaction.atomic():
                session = Session.objects.select_for_update().get(pk=pk)
                act = activity.rebuild(session, save=not opts["dry_run"])
            seen += 1
            if act.changed:
                changed += 1
                d = act.d
                self.stdout.write(
                    f"{pk} repos={d.get('repos', [])} prs={len(d.get('prs', []))} "
                    f"paths={len(d.get('paths', {}))} mcp={sum(d.get('mcp_tools', {}).values())}")
        verb = "would change" if opts["dry_run"] else "changed"
        self.stdout.write(self.style.SUCCESS(f"{seen} session(s) scanned, {changed} {verb}"))

"""Is the fleet brain alive? Print per-agent people-brain coverage for a workspace.

    manage.py people_coverage --workspace connect --days 7 [--json]

The same numbers and verdict as `GET /api/people/coverage/` (`apps/contacts/
coverage.py`). Exits 1 when the workspace is unhealthy, so a cron or a check
can be loud about a dead brain without parsing the output.
"""
from __future__ import annotations

import json

from django.core.management.base import BaseCommand, CommandError

from apps.contacts import coverage


class Command(BaseCommand):
    help = "Per-agent coverage of the people brain (fleet brain v1.1, canopy#804)."

    def add_arguments(self, parser):
        parser.add_argument("--workspace", required=True)
        parser.add_argument("--days", type=int, default=7)
        parser.add_argument("--json", action="store_true", help="Print the raw JSON.")

    def handle(self, *args, workspace, days, json: bool = False, **opts):  # noqa: A002
        from apps.workspaces.models import Workspace

        if not Workspace.objects.filter(pk=workspace).exists():
            raise CommandError(f"no workspace {workspace!r}")
        report = coverage.workspace_coverage(workspace, days=days)
        if json:
            self.stdout.write(_dumps(report))
        else:
            self._table(report)
        if not report["healthy"]:
            raise SystemExit(1)

    def _table(self, report: dict) -> None:
        w = self.stdout.write
        verdict = "HEALTHY" if report["healthy"] else "UNHEALTHY"
        w(f"people brain — {report['workspace']}, last {report['days']}d: {verdict}")
        w(f"rule: {report['rule']}")
        w(f"{'agent':<14}{'human':>6}{'ctx':>5}{'facts':>6}{'ppl':>5}  verdict")
        for r in report["agents"]:
            ok = "ok" if r["healthy"] else "UNHEALTHY"
            w(f"{r['agent']:<14}{r['human_turns']:>6}{r['human_turns_with_context']:>5}"
              f"{r['facts_written']:>6}{r['people']:>5}  {ok}"
              + (f" — {'; '.join(r['reasons'])}" if r["reasons"] else ""))
        w("columns: human turns, with context, facts recorded in-session, people")


def _dumps(obj) -> str:
    return json.dumps(obj, indent=2, sort_keys=True)

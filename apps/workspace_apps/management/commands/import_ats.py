"""Move a workspace's hiring history out of the old ATS (apps/workspace_apps/ats_import.py).

    manage.py import_ats --workspace dimagi --app hiring --out /data/ats --extract
    manage.py import_ats --workspace dimagi --app hiring --out /data/ats --dry-run
    manage.py import_ats --workspace dimagi --app hiring --out /data/ats --load

`--extract` crawls the export API into the output directory (resumable: run it again
after an interruption and it continues). `--load` maps that archive into the
installation's records (idempotent: run it again after a delta crawl). `--dry-run`
does the whole load and rolls it back, printing what it would write.

The API comes from ATS_BASE_URL / ATS_TOKEN_URL and credentials from
ATS_CLIENT_ID / ATS_CLIENT_SECRET in the environment — never from argv, where
they would land in shell history and `ps`.
"""
from __future__ import annotations

import json
import os
from pathlib import Path

from django.core.management.base import BaseCommand, CommandError

from apps.workspace_apps import ats_import
from apps.workspace_apps.models import AppInstallation


class Command(BaseCommand):
    help = "Extract an outgoing ATS account over the export API and/or load the archive into a Hiring app installation."

    def add_arguments(self, parser):
        parser.add_argument("--workspace", required=True, help="workspace slug, e.g. dimagi")
        parser.add_argument("--app", default="hiring", help="the installation's key (default: hiring)")
        parser.add_argument("--out", required=True, help="the archive directory (kept permanently)")
        parser.add_argument("--extract", action="store_true", help="crawl the old ATS into --out")
        parser.add_argument("--load", action="store_true", help="load --out into the installation")
        parser.add_argument("--dry-run", action="store_true", help="load, report, and roll back")
        parser.add_argument("--only", default="", help="extract only these endpoints (comma-separated)")
        parser.add_argument("--endpoints", default="", help="JSON file overriding endpoint paths")
        parser.add_argument("--base-url", default=os.environ.get("ATS_BASE_URL", ats_import.DEFAULT_BASE_URL))
        parser.add_argument("--token-url", default=os.environ.get("ATS_TOKEN_URL", ats_import.DEFAULT_TOKEN_URL))

    def handle(self, *args, workspace, app, out, extract=False, load=False, dry_run=False, only="",
               endpoints="", base_url=ats_import.DEFAULT_BASE_URL, token_url=ats_import.DEFAULT_TOKEN_URL, **opts):
        if not (extract or load or dry_run):
            raise CommandError("say what to do: --extract, --load and/or --dry-run")
        out_dir = Path(out).expanduser()
        inst = AppInstallation.objects.filter(workspace_id=workspace, key=app).first()
        if inst is None:
            raise CommandError(f"workspace {workspace!r} has no app {app!r} installed")
        if extract:
            self._extract(out_dir, only, endpoints, base_url, token_url)
        if load or dry_run:
            try:
                report = ats_import.Loader(inst, out_dir, dry_run=dry_run, log=self.stdout.write).run()
            except ValueError as e:
                raise CommandError(str(e)) from None
            self.stdout.write(ats_import.format_report(report))

    def _extract(self, out_dir: Path, only: str, endpoints: str, base_url: str, token_url: str) -> None:
        paths = dict(ats_import.ENDPOINTS)
        if endpoints:
            paths.update(json.loads(Path(endpoints).read_text()))
        try:
            client = ats_import.ExportClient(client_id=os.environ.get("ATS_CLIENT_ID", ""),
                                      client_secret=os.environ.get("ATS_CLIENT_SECRET", ""),
                                      base_url=base_url, token_url=token_url)
            ex = ats_import.Extractor(client, out_dir, endpoints=paths, only=[s for s in only.split(",") if s] or None,
                              log=self.stdout.write)
            summary = ex.run()
        except (ats_import.ExportError, ValueError) as e:
            raise CommandError(str(e)) from None
        failed = {k: v["status"] for k, v in summary.items() if v["status"] != "complete"}
        self.stdout.write(f"{client.requests} requests; attachments: "
                          f"{sum(1 for a in ex.files.manifest.values() if a.get('status') == 'ok')} downloaded, "
                          f"{sum(1 for a in ex.files.manifest.values() if a.get('status') != 'ok')} failed")
        if failed:
            self.stdout.write(self.style.WARNING(f"not complete: {failed}"))

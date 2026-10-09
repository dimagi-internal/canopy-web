"""Pre-warm the plugin marketplace: build every agent's archive at its repo head.

The marketplace builds on read within a time budget, so this is optional — run it
after a burst of merges, or to see why an agent is missing from a workspace's
marketplace (a build failure prints its reason here).

    manage.py build_plugin_archives [--workspace <slug>]
"""
from __future__ import annotations

from django.core.cache import cache
from django.core.management.base import BaseCommand

from apps.agents.delegations import agent_repo
from apps.plugins import services
from apps.workspaces.models import Workspace


class Command(BaseCommand):
    help = "Build each agent's plugin archive at the head of its repo ref."

    def add_arguments(self, parser):
        parser.add_argument("--workspace", help="Only this workspace's agents.")

    def handle(self, *args, workspace=None, **options):
        spaces = Workspace.objects.filter(slug=workspace) if workspace else Workspace.objects.all()
        for ws in spaces.order_by("slug"):
            for agent in services.workspace_agents(ws):
                repo, ref = agent_repo(agent), agent.repo_ref or "main"
                cache.delete(services._head_key(repo, ref))
                try:
                    token = services.github_token(agent)
                    archive = services.build(agent, services.current_head(agent, token), token)
                except Exception as exc:  # noqa: BLE001 — report and carry on
                    self.stdout.write(f"{ws.slug}/{agent.slug}: FAILED {exc}")
                    continue
                self.stdout.write(
                    f"{ws.slug}/{agent.slug}: {archive.plugin_name} {archive.version} "
                    f"({archive.size_bytes} bytes, sha256 {archive.sha256[:12]})")

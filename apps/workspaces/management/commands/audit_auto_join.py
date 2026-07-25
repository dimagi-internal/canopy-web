"""Audit (and optionally repair) `Workspace.auto_join_domains`.

`dimagi` is the only workspace that should ever auto-join domain teammates
on login (see `services.ensure_default_workspace` + CLAUDE.md's "Multi-tenancy"
notes). Every other workspace's `auto_join_domains` should be empty — a
non-empty value there grants DOMAIN-WIDE editor standing to anyone with that
email domain, so this is a checkable production posture, not folklore.

Usage:
    uv run python manage.py audit_auto_join            # report only, never mutates
    uv run python manage.py audit_auto_join --fix       # clears every non-dimagi
                                                          # workspace's auto_join_domains

Safe to run repeatedly: `--fix` is idempotent — a second run finds nothing
left to clear.
"""
from __future__ import annotations

from django.core.management.base import BaseCommand

from apps.workspaces.models import Workspace
from apps.workspaces.services import DEFAULT_WORKSPACE_SLUG


class Command(BaseCommand):
    help = "Audit workspaces for non-empty auto_join_domains; --fix clears every one except dimagi."

    def add_arguments(self, parser):
        parser.add_argument(
            "--fix",
            action="store_true",
            help="Clear auto_join_domains on every workspace except dimagi.",
        )

    def handle(self, *args, **opts):
        fix = opts["fix"]
        offenders = (
            Workspace.objects.exclude(auto_join_domains=[])
            .order_by("slug")
        )

        if not offenders.exists():
            self.stdout.write(self.style.SUCCESS("No workspaces have auto_join_domains set."))
            return

        self.stdout.write("Workspaces with non-empty auto_join_domains:")
        cleared = []
        for ws in offenders:
            member_count = ws.memberships.count()
            is_dimagi = ws.slug == DEFAULT_WORKSPACE_SLUG
            domains = list(ws.auto_join_domains)  # snapshot before any mutation below

            if is_dimagi:
                status = "OK — the one expected auto-join workspace"
            elif fix:
                ws.auto_join_domains = []
                ws.save(update_fields=["auto_join_domains"])
                cleared.append(ws.slug)
                status = "cleared"
            else:
                status = "VIOLATION — invite-only workspace must not auto-join; rerun with --fix"

            self.stdout.write(
                f"  slug={ws.slug!r} domains={domains!r} members={member_count} — {status}"
            )

        if fix:
            if cleared:
                self.stdout.write(self.style.SUCCESS(
                    f"Cleared auto_join_domains on {len(cleared)} workspace(s): {', '.join(cleared)}"
                ))
            else:
                self.stdout.write(self.style.SUCCESS("Nothing to fix — only dimagi has auto_join_domains."))
        else:
            non_dimagi = [ws.slug for ws in offenders if ws.slug != DEFAULT_WORKSPACE_SLUG]
            if non_dimagi:
                self.stdout.write(self.style.WARNING(
                    f"{len(non_dimagi)} workspace(s) violate the auto-join rule: {', '.join(non_dimagi)}. "
                    "Rerun with --fix to clear them."
                ))

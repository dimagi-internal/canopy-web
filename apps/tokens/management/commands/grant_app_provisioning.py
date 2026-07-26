"""Grant or change tenant-scoped provisioning on an EXISTING AppCredential.

`create_app_credential` hard-fails on a duplicate --name, so it cannot be
used to grant provisioning to an already-registered credential (e.g. the
production cutover: granting the live `ace-web` credential access to
`connect` after it was registered without one). Before this command, that
meant a hand-typed `.update()` in a prod shell — the one route with none of
create_app_credential's validation, one typo from the wrong slug.

Usage:
    uv run python manage.py grant_app_provisioning --name ace-web --workspace connect --role editor

See docs/superpowers/plans/2026-07-26-tenant-scoped-provisioning.md (F5).
"""
from __future__ import annotations

from django.core.management.base import BaseCommand, CommandError

from apps.tokens.models import AppCredential
from apps.workspaces.models import Workspace, WorkspaceMembership


class Command(BaseCommand):
    help = "Grant or change an EXISTING AppCredential's tenant-provisioning power."

    def add_arguments(self, parser):
        parser.add_argument("--name", required=True, help="Existing credential name.")
        parser.add_argument("--workspace", required=True,
                            help="Workspace slug to grant provisioning into.")
        parser.add_argument("--role", default=WorkspaceMembership.EDITOR,
                            help="Role granted on first provisioning (viewer|editor; "
                                 "never owner). Default: editor.")

    def handle(self, *args, **opts):
        name = opts["name"].strip()
        try:
            cred = AppCredential.objects.get(name=name)
        except AppCredential.DoesNotExist:
            raise CommandError(
                f"credential {name!r} does not exist — use create_app_credential to "
                "register it first"
            )

        workspace_slug = opts["workspace"].strip()
        try:
            ws = Workspace.objects.get(slug=workspace_slug)
        except Workspace.DoesNotExist:
            raise CommandError(f"workspace {workspace_slug!r} does not exist")

        role = opts["role"].strip().lower()
        valid_roles = dict(AppCredential.PROVISION_ROLE_CHOICES)
        if role not in valid_roles:
            raise CommandError(f"--role must be one of {sorted(valid_roles)}, got {role!r}")

        before_workspace = cred.provision_workspace_id
        before_role = cred.provision_role
        cred.provision_workspace = ws
        cred.provision_role = role
        cred.save(update_fields=["provision_workspace", "provision_role"])

        self.stdout.write(self.style.SUCCESS(
            f"{name!r}: provisioning changed "
            f"{before_workspace!r}/{before_role!r} -> {ws.slug!r}/{role!r}"
        ))
        self.stdout.write(
            "Note: this does NOT retroactively change any membership already "
            "granted under the previous workspace/role — existing rows are untouched."
        )

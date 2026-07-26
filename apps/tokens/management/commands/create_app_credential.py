"""Register an embedding application for token exchange.

Usage:
    uv run python manage.py create_app_credential --name ace-web --domains dimagi.com,dimagi-associate.com

    # Grant tenant-scoped provisioning power (see docs/superpowers/plans/
    # 2026-07-26-tenant-scoped-provisioning.md): the app's exchange calls may
    # add allowlisted-domain users to this ONE workspace, at this role.
    # --role may never be "owner".
    uv run python manage.py create_app_credential --name ace-web --domains dimagi.com \\
        --workspace connect --role editor
"""
from __future__ import annotations

from django.core.management.base import BaseCommand, CommandError

from apps.tokens.models import AppCredential
from apps.workspaces.models import Workspace, WorkspaceMembership


class Command(BaseCommand):
    help = "Register an AppCredential for on-behalf-of token exchange."

    def add_arguments(self, parser):
        parser.add_argument("--name", required=True)
        parser.add_argument("--domains", required=True,
                            help="Comma-separated email domains this app may delegate for.")
        parser.add_argument("--workspace",
                            help="Workspace slug this credential may provision users into. "
                                 "Omit for no provisioning power (the default).")
        parser.add_argument("--role", default=WorkspaceMembership.EDITOR,
                            help="Role granted on first provisioning (viewer|editor; "
                                 "never owner). Default: editor. Ignored without --workspace.")

    def handle(self, *args, **opts):
        name = opts["name"].strip()
        domains = [d.strip().lower() for d in opts["domains"].split(",") if d.strip()]
        if not domains:
            raise CommandError("--domains cannot be empty")
        if AppCredential.objects.filter(name=name).exists():
            raise CommandError(f"credential {name!r} already exists — revoke it first to rotate")

        provision_workspace = None
        workspace_slug = opts.get("workspace")
        role = opts["role"].strip().lower()
        if workspace_slug:
            try:
                provision_workspace = Workspace.objects.get(slug=workspace_slug)
            except Workspace.DoesNotExist:
                raise CommandError(f"workspace {workspace_slug!r} does not exist")
            if role == WorkspaceMembership.OWNER:
                raise CommandError(
                    "--role owner is not permitted — an app credential must never "
                    "mint an administrator of a workspace"
                )
            if role not in dict(AppCredential.PROVISION_ROLE_CHOICES):
                raise CommandError(f"--role must be one of viewer|editor, got {role!r}")

        raw, cred = AppCredential.create_credential(
            name=name, domains=domains, created_by=None,
            provision_workspace=provision_workspace,
            provision_role=role if provision_workspace else WorkspaceMembership.EDITOR,
        )
        self.stdout.write(self.style.SUCCESS(f"Registered app credential {name!r} (id={cred.pk})"))
        if provision_workspace:
            self.stdout.write(
                f"Grants provisioning into workspace {provision_workspace.slug!r} as {role!r}."
            )
        else:
            self.stdout.write("No provisioning power granted (no --workspace given).")
        self.stdout.write("\nCapture this once — it's never stored on the server:\n")
        self.stdout.write(raw)

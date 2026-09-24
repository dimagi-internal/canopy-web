"""Register a connected site.

Usage:
    uv run python manage.py create_app_credential --workspace connect --name ace-web

The credential identifies the SITE. What it can then do — vouch for a visitor
with a signed assertion, frame the embed shell, offer an agent — is granted
separately, from the workspace's Connected sites page or the `grant_app_*`
commands, so registering one hands out nothing on its own.

It used to take `--domains` (and `--workspace`/`--role`), which granted the
power to mint a token for any address in those domains and create the user as a
side effect. That endpoint is gone (2026-09-22); a site now proves who its
visitor is with a signature, and canopy never creates an account.
"""
from __future__ import annotations

from django.core.management.base import BaseCommand, CommandError

from apps.tokens.models import AppCredential


class Command(BaseCommand):
    help = "Register an AppCredential for a connected site."

    def add_arguments(self, parser):
        parser.add_argument("--name", required=True)
        parser.add_argument("--workspace", required=True,
                            help="Tenant the site belongs to — each tenant registers its own.")

    def handle(self, *args, **opts):
        name = opts["name"].strip()
        ws = opts["workspace"].strip()
        if AppCredential.objects.filter(name=name, workspace_id=ws,
                                        revoked_at__isnull=True).exists():
            raise CommandError(
                f"{ws} already has a site named {name!r}"
            )
        cred = AppCredential.create_credential(name=name, created_by=None, workspace=ws)
        self.stdout.write(self.style.SUCCESS(f"Registered site {name!r} in {ws} (id={cred.pk})"))

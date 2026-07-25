"""Register an embedding application for token exchange.

Usage:
    uv run python manage.py create_app_credential --name ace-web --domains dimagi.com,dimagi-associate.com
"""
from __future__ import annotations

from django.core.management.base import BaseCommand, CommandError

from apps.tokens.models import AppCredential


class Command(BaseCommand):
    help = "Register an AppCredential for on-behalf-of token exchange."

    def add_arguments(self, parser):
        parser.add_argument("--name", required=True)
        parser.add_argument("--domains", required=True,
                            help="Comma-separated email domains this app may delegate for.")

    def handle(self, *args, **opts):
        name = opts["name"].strip()
        domains = [d.strip().lower() for d in opts["domains"].split(",") if d.strip()]
        if not domains:
            raise CommandError("--domains cannot be empty")
        if AppCredential.objects.filter(name=name).exists():
            raise CommandError(f"credential {name!r} already exists — revoke it first to rotate")
        raw, cred = AppCredential.create_credential(name=name, domains=domains, created_by=None)
        self.stdout.write(self.style.SUCCESS(f"Registered app credential {name!r} (id={cred.pk})"))
        self.stdout.write("\nCapture this once — it's never stored on the server:\n")
        self.stdout.write(raw)

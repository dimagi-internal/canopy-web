"""Mint a Personal Access Token for a user.

Usage:
    uv run python manage.py create_token --email ace@dimagi-ai.com --label "smoke-script"
    uv run python manage.py create_token --email ace@dimagi-ai.com --label "cloud runner" --ttl-days 0

Prints the raw token to stdout. Capture it once — it isn't stored.

Tokens expire after settings.PAT_DEFAULT_TTL_DAYS (180) by default. `--ttl-days 0`
mints a non-expiring token, matching the labs `mcp_create_token --ttl-days 0`
convention.
"""
from __future__ import annotations

from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand, CommandError

from apps.tokens.models import PersonalToken


class Command(BaseCommand):
    help = "Mint a Personal Access Token for a user."

    def add_arguments(self, parser):
        parser.add_argument("--email", required=True, help="User email address.")
        parser.add_argument("--label", required=True, help="Human-readable purpose for this token.")
        parser.add_argument(
            "--create-user",
            action="store_true",
            help="Create the user if they don't exist (useful for CI bootstrap).",
        )
        parser.add_argument(
            "--ttl-days",
            type=int,
            default=None,
            help=(
                "Days until the token expires. Omit for the server default "
                "(PAT_DEFAULT_TTL_DAYS, 180). 0 mints a token that never expires."
            ),
        )

    def handle(self, *args, **opts):
        email = opts["email"].strip().lower()
        label = opts["label"].strip()
        if not label:
            raise CommandError("--label cannot be empty")

        user_model = get_user_model()
        user = user_model.objects.filter(email__iexact=email).first()
        if user is None:
            if opts["create_user"]:
                user = user_model.objects.create_user(username=email, email=email)
                self.stdout.write(self.style.WARNING(f"Created user {email} (id={user.pk})"))
            else:
                raise CommandError(
                    f"No user with email {email!r}. Pass --create-user to create one."
                )

        ttl_days = opts["ttl_days"]
        if ttl_days is not None and ttl_days < 0:
            raise CommandError("--ttl-days cannot be negative (0 means never expires)")

        raw, token = PersonalToken.create_for_user(user=user, label=label, ttl_days=ttl_days)
        expiry = (
            "never expires"
            if token.expires_at is None
            else f"expires {token.expires_at:%Y-%m-%d}"
        )
        self.stdout.write(
            self.style.SUCCESS(f"Minted token id={token.pk} for {user.email} ({expiry})")
        )
        self.stdout.write("")
        self.stdout.write("Capture this once — it's never stored on the server:")
        self.stdout.write("")
        self.stdout.write(raw)

"""Register (or remove) an origin allowed to frame an app's embed shell.

`allowed_frame_origins` is the only thing standing between an
`X-Frame-Options`-exempt page and being frameable by any site on the internet,
so it must not be edited by hand in a prod shell — the same argument as
`grant_app_provisioning`, with a worse failure: a typo here does not break a
grant, it silently fails to protect one.

The value is validated before it is stored (`is_valid_frame_origin`), so the
refusal arrives while an operator is looking at the terminal rather than as a
`frame-ancestors` entry the browser quietly ignores. `AppCredential.frame_origins()`
re-validates on read, so the two together mean no path can install a permissive
directive.

Usage:
    uv run python manage.py grant_app_frame_origin --name connect-labs \\
        --origin https://labs.connect.dimagi.com
    uv run python manage.py grant_app_frame_origin --name connect-labs \\
        --origin http://localhost:8000 --remove
    uv run python manage.py grant_app_frame_origin --name connect-labs --list
"""
from __future__ import annotations

from django.core.management.base import BaseCommand, CommandError

from apps.tokens.models import AppCredential, is_valid_frame_origin


class Command(BaseCommand):
    help = "Register or remove an origin allowed to frame an app's embed shell."

    def add_arguments(self, parser):
        parser.add_argument("--name", required=True, help="Existing AppCredential name.")
        parser.add_argument("--origin", help="Origin, e.g. https://labs.connect.dimagi.com")
        parser.add_argument("--remove", action="store_true", help="Remove instead of add.")
        parser.add_argument("--list", action="store_true", help="Show current origins and exit.")

    def handle(self, *args, **opts):
        name = opts["name"].strip()
        try:
            cred = AppCredential.objects.get(name=name)
        except AppCredential.DoesNotExist:
            raise CommandError(
                f"credential {name!r} does not exist — use create_app_credential first"
            )

        stored = list(cred.allowed_frame_origins or [])

        if opts["list"]:
            if not stored:
                self.stdout.write(
                    f"{name!r} has NO frame origins — /embed/chat?app={name} returns 404 "
                    "for everyone. That is deliberate: an exempt page with no "
                    "frame-ancestors would be frameable by any site."
                )
                return
            for origin in stored:
                ok = "" if is_valid_frame_origin(origin) else "  [IGNORED — invalid]"
                self.stdout.write(f"  {origin}{ok}")
            ignored = [o for o in stored if not is_valid_frame_origin(o)]
            if ignored:
                self.stdout.write(self.style.WARNING(
                    f"\n{len(ignored)} stored value(s) are not valid origins and are "
                    "dropped when the header is built. Remove them so the column "
                    "matches the policy actually served."
                ))
            return

        origin = (opts.get("origin") or "").strip()
        if not origin:
            raise CommandError("--origin is required unless you pass --list")

        if opts["remove"]:
            if origin not in stored:
                self.stdout.write(f"{name!r} does not list {origin!r}; nothing to do")
                return
            remaining = [o for o in stored if o != origin]
            cred.allowed_frame_origins = remaining
            cred.save(update_fields=["allowed_frame_origins"])
            self.stdout.write(self.style.SUCCESS(f"{name!r}: removed {origin!r}"))
            if not [o for o in remaining if is_valid_frame_origin(o)]:
                self.stdout.write(self.style.WARNING(
                    f"{name!r} now has no valid frame origins — its embed shell will "
                    "404 until one is added. Any page already embedding it will stop "
                    "loading the widget."
                ))
            return

        if not is_valid_frame_origin(origin):
            raise CommandError(
                f"{origin!r} is not a valid frame origin. Expected scheme + host + "
                "optional port, e.g. https://labs.connect.dimagi.com or "
                "http://localhost:8000 — no path, no wildcard. A wildcard is refused "
                "on purpose: frame-ancestors exists to enumerate the set, and '*' "
                "would restore the very exposure X-Frame-Options was preventing."
            )
        if origin in stored:
            self.stdout.write(f"{name!r} already lists {origin!r}; no change")
            return

        cred.allowed_frame_origins = stored + [origin]
        cred.save(update_fields=["allowed_frame_origins"])
        self.stdout.write(self.style.SUCCESS(f"{name!r}: may now be framed by {origin!r}"))

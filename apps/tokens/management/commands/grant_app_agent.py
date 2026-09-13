"""Allow (or stop allowing) an embedding app to offer a given agent.

`AppCredentialAgent` is what `GET /api/embed/agents` intersects with the
caller's own memberships, so without this command the only way to populate it
is a hand-typed `.create()` in a prod shell — the same gap
`grant_app_provisioning` exists to close for `provision_workspace`, and with
the same hazard: one mistyped slug silently offers the wrong agent to a host's
whole user base.

Usage:
    uv run python manage.py grant_app_agent --name connect-labs --agent labs-helper
    uv run python manage.py grant_app_agent --name connect-labs --agent labs-helper --revoke
    uv run python manage.py grant_app_agent --name connect-labs --list

Granting is idempotent — re-running is a no-op rather than an IntegrityError,
because the operational shape here is "make sure this is allowed", often from a
script.
"""
from __future__ import annotations

from django.core.management.base import BaseCommand, CommandError

from apps.agents.models import Agent
from apps.tokens.models import AppCredential, AppCredentialAgent


class Command(BaseCommand):
    help = "Allow or revoke one agent for an embedding app (see GET /api/embed/agents)."

    def add_arguments(self, parser):
        parser.add_argument("--name", required=True, help="Existing AppCredential name.")
        parser.add_argument("--agent", help="Agent slug to allow (or revoke).")
        parser.add_argument("--revoke", action="store_true",
                            help="Remove the grant instead of adding it.")
        parser.add_argument("--list", action="store_true",
                            help="Show what this app may currently offer, and exit.")

    def handle(self, *args, **opts):
        name = opts["name"].strip()
        try:
            cred = AppCredential.objects.get(name=name)
        except AppCredential.DoesNotExist:
            raise CommandError(
                f"credential {name!r} does not exist — use create_app_credential to "
                "register it first"
            )

        if opts["list"]:
            rows = (
                AppCredentialAgent.objects.filter(app=cred)
                .select_related("agent")
                .order_by("agent__slug")
            )
            if not rows:
                self.stdout.write(
                    f"{name!r} may offer NO agents — /api/embed/agents returns [] for "
                    "every caller (fail-closed, same as an empty allowed_delegation_domains)."
                )
                return
            self.stdout.write(f"{name!r} may offer:")
            for row in rows:
                self.stdout.write(f"  {row.agent.slug}  (tenant: {row.agent.workspace_id})")
            self.stdout.write(
                "\nA caller still only sees the ones whose tenant they belong to — "
                "the grant is one half of the intersection, not the whole answer."
            )
            return

        agent_slug = (opts.get("agent") or "").strip()
        if not agent_slug:
            raise CommandError("--agent is required unless you pass --list")
        try:
            agent = Agent.objects.get(slug=agent_slug)
        except Agent.DoesNotExist:
            raise CommandError(f"agent {agent_slug!r} does not exist")

        if opts["revoke"]:
            deleted, _ = AppCredentialAgent.objects.filter(app=cred, agent=agent).delete()
            if deleted:
                self.stdout.write(self.style.SUCCESS(
                    f"{name!r} may no longer offer {agent_slug!r}"
                ))
                self.stdout.write(
                    "Existing sessions with that agent are untouched — this governs "
                    "what the picker OFFERS, not what already exists."
                )
            else:
                self.stdout.write(f"{name!r} was not offering {agent_slug!r}; nothing to do")
            return

        _row, created = AppCredentialAgent.objects.get_or_create(app=cred, agent=agent)
        if created:
            self.stdout.write(self.style.SUCCESS(
                f"{name!r} may now offer {agent_slug!r} (tenant: {agent.workspace_id})"
            ))
            self.stdout.write(
                "Only to callers who are members of that tenant — the endpoint "
                "intersects this grant with the user's own memberships."
            )
        else:
            self.stdout.write(f"{name!r} already offers {agent_slug!r}; no change")

"""List agents on the canopy-web vault that are still custodial.

Zero-knowledge is the canopy-web vault's only mode (Jonathan, 2026-10-17), but
agents switched before that hold values canopy-web can read. This reports them
— it never converts: `make_zero_knowledge` (or a key holder's browser) does
that, deliberately, one agent at a time.

    python manage.py list_custodial_vault_agents [--json]
"""
from __future__ import annotations

import json

from django.core.management.base import BaseCommand

from apps.agents.models import Agent, AgentCredential


def custodial_vault_agents() -> list[dict]:
    """`[{slug, owner, values, zk_enabled}]`: every agent resolving from
    canopy-web that is not zero-knowledge or still holds a custodial value."""
    out = []
    for agent in Agent.objects.filter(credential_source=Agent.CANOPY_WEB).select_related("owner").order_by("slug"):
        custodial = [c.name for c in AgentCredential.objects.filter(agent=agent).order_by("name") if not c.is_zk]
        if agent.zk_enabled and not custodial:
            continue
        out.append({"slug": agent.slug, "owner": getattr(agent.owner, "email", "") or "",
                    "zk_enabled": agent.zk_enabled, "custodial_values": custodial})
    return out


class Command(BaseCommand):
    help = "List canopy-web-vault agents that are still custodial (report only; converts nothing)."

    def add_arguments(self, parser):
        parser.add_argument("--json", action="store_true", help="Print JSON instead of lines.")

    def handle(self, *args, **opts):
        rows = custodial_vault_agents()
        if opts["json"]:
            self.stdout.write(json.dumps(rows, indent=2))
            return
        if not rows:
            self.stdout.write("No custodial agents on the canopy-web vault.")
            return
        for r in rows:
            state = "zero-knowledge with custodial leftovers" if r["zk_enabled"] else "custodial"
            names = ", ".join(r["custodial_values"]) or "(no values)"
            self.stdout.write(f"{r['slug']}\t{r['owner']}\t{state}\t{names}")

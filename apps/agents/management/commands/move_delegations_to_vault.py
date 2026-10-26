"""Move each GitHub token a person delegated to an agent from `AgentDelegation` into the
agent's vault, as a delegated entry sealed to the machines that run the agent.

canopy-web decrypts each token in force ONCE, seals it (`accounts.seal`: to every
machine that runs the agent, and to the person's own vault key) and deletes the row in
the same transaction. After that canopy-web cannot read it. A row that is not in force
(left by a former owner or admin) is deleted, not moved. An agent with no machine that
can open one keeps its row, which still serves, and is listed for its owner.

Dry run by default:

    python manage.py move_delegations_to_vault           # what it would do
    python manage.py move_delegations_to_vault --apply

Spec: docs/superpowers/specs/2026-10-26-delegated-credentials-in-the-vault-design.md
"""
from __future__ import annotations

from django.core.management.base import BaseCommand
from django.db import transaction

from apps.agents.credentials import accounts, delegations
from apps.agents.credentials.zk import ZkError
from apps.agents.models import AgentCredential, AgentDelegation
from apps.common.encryption import decrypt_secret


def move(row: AgentDelegation, *, apply: bool) -> str:
    """What happened to one GitHub row, in a line."""
    agent = row.agent
    label = f"{agent.slug} ← {row.user.email}"
    if delegations.github_entry(agent) is not None:
        if apply:
            row.delete()
        return f"{label}: already in the vault — row deleted"
    if delegations.delegation_for(agent) != row:
        if apply:
            row.delete()
        return f"{label}: not in force — row deleted, not moved"
    if not apply:
        machines = sorted({k.runner.name for k in accounts.delegated_devices(agent)})
        if not machines:
            return f"{label}: KEPT — no machine that runs {agent.slug} can open one yet"
        return f"{label}: would move, sealed to {', '.join(machines)}"
    meta = row.meta or {}
    with transaction.atomic():
        try:
            value_zk, machines = accounts.seal(agent, accounts.GITHUB_ENTRY, decrypt_secret(row.secret_enc),
                                               row.user)
        except ZkError as exc:
            return f"{label}: KEPT — {exc}"
        AgentCredential.objects.update_or_create(
            agent=agent, name=accounts.GITHUB_ENTRY,
            defaults={
                "value_zk": value_zk, "value_enc": "", "kv": agent.zk_kv, "updated_by": row.user,
                "account": "delegated", "delegated_by": row.user, "delegated_by_agent": None,
                "provider": "github",
                "identity": {k: meta.get(k) for k in ("login", "user_id", "name")},
                "purpose": f"{agent.slug} pushes and opens pull requests"
                           + (f" on {delegations.agent_repo(agent)}" if delegations.agent_repo(agent) else ""),
                "scopes": [c.get("repo") for c in meta.get("checks", []) if c.get("repo")],
                "provider_expires_at": row.expires_at,
                "checks": meta.get("checks", []), "checked_by": "server",
            })
        row.delete()
    return f"{label}: moved, sealed to {', '.join(machines)}"


class Command(BaseCommand):
    help = "Move delegated GitHub tokens from AgentDelegation into each agent's vault (dry run unless --apply)."

    def add_arguments(self, parser):
        parser.add_argument("--apply", action="store_true", help="Move them (default: report only).")

    def handle(self, *args, **opts):
        rows = (AgentDelegation.objects.filter(service=AgentDelegation.GITHUB)
                .exclude(secret_enc="").select_related("agent__owner", "user").order_by("agent__slug"))
        for row in rows:
            self.stdout.write(move(row, apply=opts["apply"]))

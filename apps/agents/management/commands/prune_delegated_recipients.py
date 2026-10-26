"""Drop, from every delegated credential's sealed value, the recipients that may no
longer hold it (`accounts.prune`). Decrypts nothing. Dry run unless --apply."""
from django.core.management.base import BaseCommand

from apps.agents.credentials import accounts, zk_seal
from apps.agents.models import AgentCredential, VaultEntryAccount


class Command(BaseCommand):
    help = "Drop machines that may no longer hold a delegated credential from its sealed value (dry run unless --apply)."

    def add_arguments(self, parser):
        parser.add_argument("--apply", action="store_true")

    def handle(self, *args, apply=False, **opts):
        entries = (AgentCredential.objects.filter(account=VaultEntryAccount.DELEGATED)
                   .select_related("agent", "delegated_by").order_by("agent__slug", "name"))
        for entry in entries:
            agent = entry.agent
            if not accounts.is_delegated(entry, agent):
                continue
            label = f"{agent.slug}/{entry.name} ← {entry.delegated_by.email if entry.delegated_by_id else '?'}"
            if apply:
                dropped = accounts.prune(entry, agent)
            else:
                allowed = {("device", k.ecdh_jkt) for k in accounts.entry_devices(entry, agent)}
                if entry.delegated_by_id:
                    allowed.add(("member", str(entry.delegated_by_id)))
                _, gone = zk_seal.keep_only(entry.value_zk, allowed)
                dropped = sorted(t for _, t in gone)
            verb = "dropped" if apply else "would drop"
            self.stdout.write(f"{label}: {verb} {', '.join(dropped)}" if dropped else f"{label}: nothing to drop")

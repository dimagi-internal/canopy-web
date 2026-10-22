"""Undo 0059 for agents whose secrets still come from 1Password.

0059 made every agent holding no canopy-web value zero-knowledge, 1Password ones
included, so their resolve demanded a device proof and `canopy agent op-token`
broke on any laptop without an approved device key — for an agent with nothing
in the canopy-web vault to protect. Turned back off only where there is no
zero-knowledge material at all: source 1Password, no AgentCredential, no vault
key (kv 0), no wrap and no endorsement. Anything holding ZK material is left as
it is. Reversible as a no-op.
"""
from django.db import migrations


def forwards(apps, schema_editor):
    Agent = apps.get_model("agents", "Agent")
    AgentCredential = apps.get_model("agents", "AgentCredential")
    AgentVaultWrap = apps.get_model("agents", "AgentVaultWrap")
    AgentVaultEndorsement = apps.get_model("agents", "AgentVaultEndorsement")
    holding = set(AgentCredential.objects.values_list("agent_id", flat=True).distinct())
    holding |= set(AgentVaultWrap.objects.values_list("agent_id", flat=True).distinct())
    holding |= set(AgentVaultEndorsement.objects.values_list("agent_id", flat=True).distinct())
    (Agent.objects.filter(zk_enabled=True, zk_kv=0, credential_source="1password")
     .exclude(pk__in=holding).update(zk_enabled=False, zk_kv=0))


class Migration(migrations.Migration):
    dependencies = [("agents", "0068_vault_endorsements_and_secret_requests")]
    operations = [migrations.RunPython(forwards, migrations.RunPython.noop)]

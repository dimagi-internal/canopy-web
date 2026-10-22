"""Agents with no 1Password vault move onto canopy vault (Jonathan, 2026-10-21).

Every agent's secrets live in canopy vault now; 1Password is legacy, kept only for
an agent that still has a vault there. An agent recorded as `1password` with no
vault at all (made before new agents were born on canopy vault) could never
resolve a secret, and its owner could not move it — `canopy vault switch` needs a
1Password vault to read from. It becomes what a new agent is: canopy vault, zero-
knowledge from birth at kv 0. Only an agent holding no canopy-web value, wrap or
endorsement is touched. Reversible as a no-op.
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
    (Agent.objects.filter(credential_source="1password", op_vault="")
     .exclude(pk__in=holding)
     .update(credential_source="canopy-web", zk_enabled=True, zk_kv=0))


class Migration(migrations.Migration):
    dependencies = [("agents", "0069_no_zk_for_1password_agents")]
    operations = [migrations.RunPython(forwards, migrations.RunPython.noop)]

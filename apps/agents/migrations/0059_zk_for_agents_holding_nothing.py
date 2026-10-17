"""Zero-knowledge is the canopy-web vault's only mode (Jonathan, 2026-10-17).

Every agent that holds no canopy-web value yet becomes zero-knowledge at kv 0
("born zero-knowledge": no vault key yet, values sealed to its readers). An
agent that already holds custodial values is left as it is — converting it
needs its plaintext sealed to readers who exist; `manage.py
list_custodial_vault_agents` lists those. Reversible as a no-op.
"""
from django.db import migrations


def forwards(apps, schema_editor):
    Agent = apps.get_model("agents", "Agent")
    AgentCredential = apps.get_model("agents", "AgentCredential")
    holding = AgentCredential.objects.values_list("agent_id", flat=True).distinct()
    Agent.objects.filter(zk_enabled=False).exclude(pk__in=holding).update(zk_enabled=True, zk_kv=0)


class Migration(migrations.Migration):
    dependencies = [("agents", "0058_project_proposals")]
    operations = [migrations.RunPython(forwards, migrations.RunPython.noop)]

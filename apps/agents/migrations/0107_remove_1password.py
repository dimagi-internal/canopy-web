"""Remove 1Password from the agent model.

Every agent's secrets live in the canopy vault; `credential_source`, the agent's
1Password vault name (`op_vault`) and its encrypted service-account key
(`op_sa_token_enc`) describe a store nothing reads. The data goes with the columns,
and a `runtime_sources` entry loses its `op://` pointer (the rest of it stays).

Not reversible: the vault names and the encrypted keys are deleted for good. The keys
themselves are revoked on the 1Password side.
"""
import logging

from django.db import migrations, models

logger = logging.getLogger(__name__)


def strip_op_pointers(apps, schema_editor):
    Agent = apps.get_model("agents", "Agent")
    leftover = list(Agent.objects.filter(credential_source="1password").values_list("slug", flat=True))
    if leftover:
        # They keep working from the canopy vault: a first stored value turns it on.
        logger.warning("0107: agents still on 1Password when it was removed: %s", ", ".join(leftover))
    for agent in Agent.objects.exclude(runtime_sources={}).only("id", "runtime_sources"):
        cleaned = {
            name: ({k: v for k, v in src.items() if k != "op"} if isinstance(src, dict) else src)
            for name, src in (agent.runtime_sources or {}).items()
        }
        if cleaned != agent.runtime_sources:
            agent.runtime_sources = cleaned
            agent.save(update_fields=["runtime_sources"])


class Migration(migrations.Migration):
    dependencies = [
        ("agents", "0106_credential_alert_context"),
    ]

    operations = [
        migrations.RunPython(strip_op_pointers, migrations.RunPython.noop),
        migrations.RemoveField(model_name="agent", name="op_vault"),
        migrations.RemoveField(model_name="agent", name="op_sa_token_enc"),
        migrations.RemoveField(model_name="agent", name="credential_source"),
        migrations.AlterField(
            model_name="agent",
            name="runtime_sources",
            field=models.JSONField(
                blank=True, default=dict,
                help_text='Per-ref value source: {"name": {"value": "literal"}}.',
            ),
        ),
    ]

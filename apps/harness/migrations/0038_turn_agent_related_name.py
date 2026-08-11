"""Take the `agent.turns` name now that AgentTurn has released it.

State-only (a related_name change emits no SQL), but the ORDERING is real: while
agents.AgentTurn still existed, two relations would have claimed `Agent.turns` and
Django's checks would refuse to start. So this waits on agents/0017, and the field
additions it accompanies do NOT — the data migration in between has to write into
them before the delete happens.
"""
import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("harness", "0037_turn_cli_session_id_turn_emdash_task_id_and_more"),
        ("agents", "0017_delete_agentturn"),
    ]

    operations = [
        migrations.AlterField(
            model_name="turn",
            name="agent",
            field=models.ForeignKey(
                blank=True, null=True,
                on_delete=django.db.models.deletion.CASCADE,
                related_name="turns", to="agents.agent",
            ),
        ),
    ]

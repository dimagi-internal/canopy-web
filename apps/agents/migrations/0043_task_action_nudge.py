"""`dispatch` → `nudge`: the "<Agent>, do this now" action is retired.

It only ever wrote a pending row the agent drained whenever it next ran, so it
started nothing. Approve now starts the work and `nudge` re-starts it on a task
already in progress (Jonathan, 2026-10-08). Per the projects-and-tasks cutover
rule there is no alias: existing `dispatch` rows are rewritten to `nudge`, the
action they meant.
"""
from django.db import migrations, models


def forwards(apps, schema_editor):
    AgentTaskAction = apps.get_model("agents", "AgentTaskAction")
    AgentTaskAction.objects.filter(action="dispatch").update(action="nudge")


def backwards(apps, schema_editor):
    AgentTaskAction = apps.get_model("agents", "AgentTaskAction")
    AgentTaskAction.objects.filter(action="nudge").update(action="dispatch")


class Migration(migrations.Migration):
    dependencies = [("agents", "0042_sender_trust")]

    operations = [
        migrations.RunPython(forwards, backwards),
        migrations.AlterField(
            model_name="agenttaskaction",
            name="action",
            field=models.CharField(
                choices=[("approve", "approve"), ("decline", "decline"), ("reply", "reply"),
                         ("nudge", "nudge"), ("done", "done")],
                max_length=10),
        ),
    ]

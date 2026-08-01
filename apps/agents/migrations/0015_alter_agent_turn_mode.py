"""Rename the non-auto turn mode: `gated` -> `manual`.

`gated` described the mechanism (an approval gate); `manual` describes what the
operator actually does, which is the word the humans running the fleet use. The
value is stored, so the choices change alone is not enough — AlterField only
rewrites Python-level choices/default and would leave every existing row saying
`gated`, which no longer validates and would render as an unknown mode in the UI.
Hence the RunPython backfill, and a reverse that puts the old value back so this
migration is not a one-way door.
"""
from django.db import migrations, models


def gated_to_manual(apps, schema_editor):
    apps.get_model("agents", "Agent").objects.filter(turn_mode="gated").update(turn_mode="manual")


def manual_to_gated(apps, schema_editor):
    apps.get_model("agents", "Agent").objects.filter(turn_mode="manual").update(turn_mode="gated")


class Migration(migrations.Migration):

    dependencies = [
        ('agents', '0014_agent_turn_mode'),
    ]

    operations = [
        migrations.AlterField(
            model_name='agent',
            name='turn_mode',
            field=models.CharField(choices=[('manual', 'manual (outbound waits for human approval)'), ('auto', 'auto (self-review-and-send, audit on the board)')], default='manual', max_length=8),
        ),
        migrations.RunPython(gated_to_manual, manual_to_gated),
    ]

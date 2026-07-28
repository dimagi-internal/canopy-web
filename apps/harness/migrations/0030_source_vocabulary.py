"""The source vocabulary (spec 2026-07-27): widen both origin columns and remap
the retired values. Widening a varchar in Postgres is metadata-only — no rewrite.

The reverse is deliberately lossy: `api` fans back out to board/manual/drill with
no way to tell which, so it maps everything back to `api` and only un-renames
canopy_scheduler. Reversing this migration restores a runnable schema, not the
exact prior labels.
"""
from django.db import migrations, models

FORWARD = {"cron": "canopy_scheduler", "manual": "api", "drill": "api", "board": "api"}
BACKWARD = {"canopy_scheduler": "cron", "canopy_web_chat": "api"}

CHOICES = [
    ("api", "API"), ("ace_web", "ace-web"),
    ("canopy_web_chat", "canopy-web chat"),
    ("canopy_scheduler", "canopy scheduler"),
    ("email", "Email"), ("slack", "Slack"),
]


def _remap(apps, mapping):
    Turn = apps.get_model("harness", "Turn")
    Item = apps.get_model("harness", "Item")
    for old, new in mapping.items():
        Turn.objects.filter(origin=old).update(origin=new)
        Item.objects.filter(origin=old).update(origin=new)


def forwards(apps, schema_editor):
    _remap(apps, FORWARD)


def backwards(apps, schema_editor):
    _remap(apps, BACKWARD)


class Migration(migrations.Migration):
    dependencies = [("harness", "0029_turntranscript_last_batch_id_and_more")]

    operations = [
        migrations.AlterField(
            model_name="turn",
            name="origin",
            field=models.CharField(max_length=32, choices=CHOICES),
        ),
        migrations.AlterField(
            model_name="item",
            name="origin",
            field=models.CharField(max_length=32, choices=CHOICES),
        ),
        migrations.RunPython(forwards, backwards),
    ]

"""A source never looked at is due from when it was made: give those rows `associate_due_at`, so `associate.run_due`
asks that one column (indexed in 0024). Its own migration, ahead of the schema change to the table
(CLAUDE.md § Migrations)."""

from django.db import migrations
from django.db.models import F


def due_when_made(apps, schema_editor):
    BrainSource = apps.get_model("brain", "BrainSource")
    BrainSource.objects.filter(associated_at__isnull=True, associate_due_at__isnull=True).update(
        associate_due_at=F("created_at"))


class Migration(migrations.Migration):

    dependencies = [
        ("brain", "0022_recency_window_merge"),
    ]

    operations = [
        migrations.RunPython(due_when_made, migrations.RunPython.noop),
    ]

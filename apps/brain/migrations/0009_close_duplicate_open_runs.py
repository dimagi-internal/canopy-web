"""Before one open run per feature and subject becomes a constraint (`0010`), any duplicate open runs left by
the old check-then-create are closed as failed, keeping the newest. Its own migration, ahead of the schema
change to the same table (CLAUDE.md § Migrations)."""
from django.db import migrations
from django.utils import timezone


def close_duplicates(apps, schema_editor):
    BrainRun = apps.get_model("brain", "BrainRun")
    seen = set()
    for run in BrainRun.objects.filter(state="open").order_by("-created_at", "-pk"):
        key = (run.capability_id, run.subject_type, run.subject)
        if key in seen:
            BrainRun.objects.filter(pk=run.pk).update(state="failed", finished_at=timezone.now())
        seen.add(key)


class Migration(migrations.Migration):

    dependencies = [
        ("brain", "0008_scheduling"),
    ]

    operations = [
        migrations.RunPython(close_duplicates, migrations.RunPython.noop),
    ]

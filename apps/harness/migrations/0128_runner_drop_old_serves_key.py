"""Drop the old `capabilities["unit"]` key 0127 left beside `serves`: every runner now
reports `serves`, and nothing reads the old key."""
from django.db import migrations

OLD, NEW = "unit", "serves"


def drop_old_key(apps, schema_editor):
    Runner = apps.get_model("harness", "Runner")
    for runner in Runner.objects.filter(capabilities__has_key=OLD).only("pk", "capabilities"):
        caps = dict(runner.capabilities or {})
        old = caps.pop(OLD)
        if isinstance(old, dict) and not caps.get(NEW):
            caps[NEW] = old
        Runner.objects.filter(pk=runner.pk).update(capabilities=caps)


class Migration(migrations.Migration):
    dependencies = [
        ("harness", "0127_runner_serves_and_invited_runners"),
    ]

    operations = [
        migrations.RunPython(drop_old_key, migrations.RunPython.noop),
    ]

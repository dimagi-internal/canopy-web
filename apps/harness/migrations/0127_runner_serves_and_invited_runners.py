"""Two names for runners on one machine: what a runner serves is
`capabilities["serves"]`, and the runners an invitation pairs are
`RunnerInvitation.runners`.

The stored capability is copied, not moved: a server still running the previous
code while this deploy rolls out reads the old key, and one of its writes would
otherwise drop what the runner serves for good. The new code reads `serves` first
and drops the old key on its next write; the follow-up removes what is left."""
from django.db import migrations

OLD, NEW = "unit", "serves"


def copy_serves(apps, schema_editor):
    Runner = apps.get_model("harness", "Runner")
    for runner in Runner.objects.filter(capabilities__has_key=OLD).only("pk", "capabilities"):
        caps = dict(runner.capabilities or {})
        if isinstance(caps.get(OLD), dict) and not caps.get(NEW):
            caps[NEW] = caps[OLD]
            Runner.objects.filter(pk=runner.pk).update(capabilities=caps)


def drop_serves(apps, schema_editor):
    Runner = apps.get_model("harness", "Runner")
    for runner in Runner.objects.filter(capabilities__has_key=NEW).only("pk", "capabilities"):
        caps = dict(runner.capabilities or {})
        value = caps.pop(NEW)
        caps.setdefault(OLD, value)
        Runner.objects.filter(pk=runner.pk).update(capabilities=caps)


class Migration(migrations.Migration):
    dependencies = [
        ("harness", "0126_runner_refresh_outcome"),
    ]

    operations = [
        migrations.RenameField(model_name="runnerinvitation", old_name="units", new_name="runners"),
        migrations.RunPython(copy_serves, drop_serves),
    ]

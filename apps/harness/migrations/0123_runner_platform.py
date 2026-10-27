"""A runner knows the operating system it said it runs (`Runner.platform`), so a
remedy can name the fix that exists there. Existing runners take it from the
latest pairing that made or reconnected them; the rest stay blank."""
from django.db import migrations, models


def backfill(apps, schema_editor):
    Runner = apps.get_model("harness", "Runner")
    RunnerPairing = apps.get_model("harness", "RunnerPairing")
    seen = set()
    rows = (RunnerPairing.objects.filter(runner__isnull=False, consumed_at__isnull=False)
            .exclude(platform="").order_by("runner_id", "-consumed_at")
            .values_list("runner_id", "platform"))
    for runner_id, platform in rows:
        if runner_id in seen:
            continue
        seen.add(runner_id)
        Runner.objects.filter(pk=runner_id).update(platform=platform)


class Migration(migrations.Migration):
    dependencies = [("harness", "0122_turn_requested_cli")]
    operations = [
        migrations.AddField(
            model_name="runner", name="platform",
            field=models.CharField(blank=True, db_default="", default="", max_length=10),
        ),
        migrations.RunPython(backfill, migrations.RunPython.noop),
    ]

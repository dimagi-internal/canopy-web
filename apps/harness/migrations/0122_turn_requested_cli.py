"""A turn's requested CLI is kept apart from the one the claim picked
(`harness.clis`), so a requeue clears the pick and keeps the request. A turn not
yet finished carries its `cli` over as its request: a queued one's `cli` could
only have come from its starter, and it is cleared to be picked again."""
from django.db import migrations, models

NOT_DONE = ["queued", "claimed", "running", "needs_human"]


def split(apps, schema_editor):
    Turn = apps.get_model("harness", "Turn")
    live = Turn.objects.filter(status__in=NOT_DONE).exclude(cli="")
    live.update(requested_cli=models.F("cli"))
    Turn.objects.filter(status="queued").exclude(cli="").update(cli="")


class Migration(migrations.Migration):
    dependencies = [("harness", "0121_runner_paired_as")]
    operations = [
        migrations.AddField(
            model_name="turn", name="requested_cli",
            field=models.CharField(blank=True, db_default="", default="", max_length=32),
        ),
        migrations.RunPython(split, migrations.RunPython.noop),
    ]

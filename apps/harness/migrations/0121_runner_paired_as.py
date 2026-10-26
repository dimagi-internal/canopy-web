from django.db import migrations, models
from django.db.models import F


def backfill(apps, schema_editor):
    # Every runner so far still has the name it paired as.
    apps.get_model("harness", "Runner").objects.filter(paired_as="").update(paired_as=F("name"))


class Migration(migrations.Migration):
    dependencies = [
        ("harness", "0120_schedule_cli"),
    ]

    operations = [
        migrations.AddField(
            model_name="runner",
            name="paired_as",
            field=models.CharField(blank=True, db_default="", default="", max_length=100),
        ),
        migrations.RunPython(backfill, migrations.RunPython.noop),
    ]

from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("harness", "0085_runner_machine_id"),
    ]

    operations = [
        migrations.AddField(
            model_name="runnerinvitation",
            name="units",
            field=models.JSONField(blank=True, default=list),
        ),
    ]

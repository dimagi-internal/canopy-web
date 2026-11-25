from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("harness", "0141_runner_sync_version"),
    ]

    operations = [
        migrations.AddField(
            model_name="runner",
            name="keeper_version",
            field=models.BigIntegerField(default=0),
        ),
    ]

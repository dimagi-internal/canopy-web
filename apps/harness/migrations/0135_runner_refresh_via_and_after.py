from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("harness", "0134_plain_jobs"),
    ]

    operations = [
        migrations.AddField(
            model_name="runner", name="refresh_requested_via",
            field=models.CharField(blank=True, default="", max_length=16),
        ),
        migrations.AddField(
            model_name="runner", name="refresh_after",
            field=models.JSONField(blank=True, default=dict),
        ),
    ]

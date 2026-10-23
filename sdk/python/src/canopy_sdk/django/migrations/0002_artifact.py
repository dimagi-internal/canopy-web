import django.utils.timezone
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [("canopy_host", "0001_initial")]

    operations = [
        migrations.CreateModel(
            name="Artifact",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("artifact_id", models.CharField(max_length=64)),
                ("scheme", models.CharField(max_length=32)),
                ("owner", models.CharField(db_index=True, max_length=255)),
                ("kind", models.CharField(blank=True, max_length=100)),
                ("tool_call_id", models.CharField(blank=True, max_length=255)),
                ("name", models.CharField(blank=True, max_length=200)),
                ("description", models.CharField(blank=True, max_length=500)),
                ("mime_type", models.CharField(default="application/json", max_length=100)),
                ("payload", models.JSONField()),
                ("created_at", models.DateTimeField(default=django.utils.timezone.now)),
                ("expires_at", models.DateTimeField(db_index=True)),
            ],
            options={"db_table": "canopy_host_artifact"},
        ),
        migrations.AddConstraint(
            model_name="artifact",
            constraint=models.UniqueConstraint(fields=("scheme", "artifact_id"),
                                               name="canopy_host_artifact_uri_unique"),
        ),
    ]

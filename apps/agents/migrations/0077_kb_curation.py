from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("agents", "0076_kb_versions_sources"),
    ]

    operations = [
        migrations.AddField(
            model_name="agentproject",
            name="kb_curation",
            field=models.JSONField(blank=True, default=dict),
        ),
    ]

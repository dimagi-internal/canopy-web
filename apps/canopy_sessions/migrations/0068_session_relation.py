from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("canopy_sessions", "0067_session_content_deleted"),
    ]

    operations = [
        migrations.AddField(
            model_name="session",
            name="relation",
            field=models.CharField(blank=True, default="", max_length=16),
        ),
    ]

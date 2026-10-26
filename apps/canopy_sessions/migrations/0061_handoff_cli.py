from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("canopy_sessions", "0060_backfill_session_cli")]
    operations = [
        migrations.AddField(
            model_name="sessionhandoff", name="to_cli",
            field=models.CharField(blank=True, db_default="", default="", max_length=32),
        ),
        migrations.AddField(
            model_name="transferrequest", name="cli",
            field=models.CharField(blank=True, db_default="", default="", max_length=32),
        ),
    ]

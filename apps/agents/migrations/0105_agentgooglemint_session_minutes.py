from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("agents", "0104_agentgooglemint_delegated"),
    ]

    operations = [
        migrations.AddField(
            model_name="agentgooglemint",
            name="session_minutes",
            field=models.PositiveSmallIntegerField(default=0),
        ),
    ]

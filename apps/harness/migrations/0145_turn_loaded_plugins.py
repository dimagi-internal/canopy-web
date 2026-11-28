from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("harness", "0144_act_as_grant_push_sent_gone"),
    ]

    operations = [
        migrations.AddField(
            model_name="turn",
            name="loaded_plugins",
            field=models.JSONField(blank=True, default=dict),
        ),
    ]

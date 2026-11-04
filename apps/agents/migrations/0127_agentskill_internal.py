from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("agents", "0126_lead_agent_may_publish_updates"),
    ]

    operations = [
        migrations.AddField(
            model_name="agentskill",
            name="internal",
            field=models.BooleanField(
                default=False, help_text="Claude loads this skill; a person is not offered it as a command."
            ),
        ),
    ]

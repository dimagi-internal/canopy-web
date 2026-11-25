from django.db import migrations, models


class Migration(migrations.Migration):
    """Browser sign-in may last until switched off: `expires_at` null is no end."""

    dependencies = [("agents", "0143_creator_agent_admins")]

    operations = [
        migrations.AlterField(
            model_name="agentbrowsersignin",
            name="expires_at",
            field=models.DateTimeField(blank=True, null=True),
        ),
    ]

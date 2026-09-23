from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("harness", "0047_caller_token"),
    ]

    operations = [
        migrations.AddField(
            model_name="agentschedule",
            name="always_run",
            field=models.BooleanField(
                default=False,
                help_text="Run a slot however late it is claimed. Off: a slot not claimed "
                "within LATE_SLOT_WINDOW_MINUTES of its time is skipped as MISSED, so a "
                "laptop reopened after a day away does not fire every schedule at once.",
            ),
        ),
    ]

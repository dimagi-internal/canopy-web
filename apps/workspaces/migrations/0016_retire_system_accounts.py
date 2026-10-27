"""Retire system accounts, step 2: drop the models (0015 deleted their users)."""
from django.db import migrations


class Migration(migrations.Migration):
    dependencies = [
        ("workspaces", "0015_delete_system_users"),
    ]

    operations = [
        migrations.RemoveConstraint(
            model_name="systemaccount",
            name="uniq_ws_system_account_name",
        ),
        migrations.RemoveConstraint(
            model_name="systemsender",
            name="uniq_system_sender",
        ),
        migrations.RemoveField(
            model_name="systemsender",
            name="account",
        ),
        migrations.DeleteModel(
            name="SystemAccount",
        ),
        migrations.DeleteModel(
            name="SystemSender",
        ),
    ]

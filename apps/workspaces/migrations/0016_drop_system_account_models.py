"""Drop SystemAccount and SystemSender, whose rows 0015 deleted.

Its own migration so it runs after 0015's transaction has committed — see 0015.
"""
from django.db import migrations


class Migration(migrations.Migration):
    dependencies = [
        ("workspaces", "0015_retire_system_accounts"),
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

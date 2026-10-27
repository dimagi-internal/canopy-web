"""Retire system accounts (docs/superpowers/specs/2026-10-27-agent-triggers-design.md).

`harness/0125` has already made each one into the inbound-mail triggers it amounted
to and moved its turns onto them. Here its login-less user is deleted — taking its
membership, the account and its sender bindings with it — and the two models go.
"""
from django.db import migrations


def delete_system_users(apps, schema_editor):
    SystemAccount = apps.get_model("workspaces", "SystemAccount")
    User = apps.get_model("auth", "User")
    ids = list(SystemAccount.objects.values_list("user_id", flat=True))
    if ids:
        User.objects.filter(pk__in=ids).delete()


class Migration(migrations.Migration):
    dependencies = [
        ("workspaces", "0014_drop_shared_1password_vault"),
        ("harness", "0125_system_accounts_to_triggers"),
        # Reads SystemAccount from the historical state, so it must run first.
        ("mail", "0008_backfill_email_session_people"),
    ]

    operations = [
        migrations.RunPython(delete_system_users, migrations.RunPython.noop),
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

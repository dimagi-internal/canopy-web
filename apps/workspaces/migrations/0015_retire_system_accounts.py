"""Retire system accounts (docs/superpowers/specs/2026-10-27-agent-triggers-design.md).

`harness/0125` has already made each one into the inbound-mail triggers it amounted
to and moved its turns onto them. Here its login-less user is deleted — taking its
membership, the account and its sender bindings with it. The two models go in
0016: on Postgres the delete leaves deferred FK trigger events pending until its
transaction commits, and ALTER TABLE on those tables refuses to run before then.
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
    ]

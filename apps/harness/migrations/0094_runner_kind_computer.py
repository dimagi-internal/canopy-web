"""The `computer` runner kind, and one for every live computer registered without a
runner app — so each can enrol a device key (`local.ensure_computer_runner`)."""

from django.db import migrations, models


def give_computers_a_runner(apps, schema_editor):
    PrivateRunner = apps.get_model("harness", "PrivateRunner")
    Runner = apps.get_model("harness", "Runner")
    for pr in PrivateRunner.objects.filter(runner__isnull=True, retired_at__isnull=True):
        pr.runner = Runner.objects.create(
            name=pr.name, kind="computer", private=True, owner_id=pr.owner_id,
            workspace=None, capabilities={})
        pr.save(update_fields=["runner"])


class Migration(migrations.Migration):
    dependencies = [
        ("harness", "0093_turn_agent_token"),
    ]

    operations = [
        migrations.AlterField(
            model_name="runner",
            name="kind",
            field=models.CharField(
                choices=[
                    ("emdash", "Emdash"),
                    ("cloud", "Cloud"),
                    ("remote", "Remote"),
                    ("computer", "Computer (Claude Code, no runner app)"),
                ],
                max_length=10,
            ),
        ),
        migrations.RunPython(give_computers_a_runner, migrations.RunPython.noop),
    ]

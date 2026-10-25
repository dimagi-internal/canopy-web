from django.db import migrations, models


def copy_vaults(apps, schema_editor):
    RunnerCredentialSet = apps.get_model("harness", "RunnerCredentialSet")
    for cset in RunnerCredentialSet.objects.exclude(vault__isnull=True):
        cset.vaults.add(cset.vault_id)


class Migration(migrations.Migration):

    dependencies = [
        ("agents", "0089_remove_kb_drive_source"),
        ("harness", "0115_runner_fleets"),
    ]

    operations = [
        migrations.AddField(
            model_name="runnercredentialset",
            name="vaults",
            field=models.ManyToManyField(blank=True, related_name="+", to="agents.vault"),
        ),
        migrations.RunPython(copy_vaults, migrations.RunPython.noop),
        migrations.RemoveField(model_name="runnercredentialset", name="vault"),
    ]

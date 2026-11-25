"""`system-learning` ships set up: operator `ada`, entry `/canopy:brain-learn` (`registry.PRESETS`), when it has no
operator yet and `ada` exists. Enabling it is then one switch plus a target. Never over an operator someone chose.

Its own migration: it updates rows other tables reference, so it runs ahead of any schema change to them."""
from django.db import migrations

PRESETS = {"system-learning": {"operator": "ada", "entry": "/canopy:brain-learn"}}


def preset(apps, schema_editor):
    caps, agents = apps.get_model("brain", "BrainCapability"), apps.get_model("agents", "Agent")
    for slug, spec in PRESETS.items():
        agent = agents.objects.filter(slug=spec["operator"]).first()
        if agent is not None:
            caps.objects.filter(slug=slug, operator__isnull=True).update(operator=agent, entry=spec["entry"])


class Migration(migrations.Migration):

    dependencies = [
        ("brain", "0016_learning_targets"),
    ]

    operations = [migrations.RunPython(preset, migrations.RunPython.noop)]

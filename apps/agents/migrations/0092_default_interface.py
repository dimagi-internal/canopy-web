from django.db import migrations, models

import apps.agents.interface_default
from apps.agents.interface_default import default_interface


def give_every_agent_an_interface(apps, schema_editor):
    """Every agent that has published nothing gets the default interface, written out.
    An agent that has published one — ACE, Hal, Eva — is not touched. The default says
    what an empty `{}` used to mean by absence, so no one's access changes."""
    Agent = apps.get_model("agents", "Agent")
    for pk in Agent.objects.filter(interface={}).values_list("pk", flat=True):
        Agent.objects.filter(pk=pk).update(interface=default_interface())


class Migration(migrations.Migration):

    dependencies = [
        ('agents', '0091_default_agent'),
    ]

    operations = [
        migrations.AlterField(
            model_name='agent',
            name='interface',
            field=models.JSONField(blank=True, default=apps.agents.interface_default.default_interface),
        ),
        migrations.RunPython(give_every_agent_an_interface, migrations.RunPython.noop),
    ]

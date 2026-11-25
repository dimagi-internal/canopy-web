# A revision records the agent that did a person's act as them, and the act-as-you
# permission it held.
import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("agents", "0139_drop_agentdefinitionblob"),
        ("harness", "0140_origin_site_message"),
    ]

    operations = [
        migrations.AddField(
            model_name="agentdefinitionrevision",
            name="acting_agent",
            field=models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL,
                                    related_name="+", to="agents.agent"),
        ),
        migrations.AddField(
            model_name="agentdefinitionrevision",
            name="act_as_grant",
            field=models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL,
                                    related_name="+", to="harness.actasgrant"),
        ),
    ]

# A revision records the agent that published it as itself, as an admin of the revision's agent.

import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('agents', '0141_agent_browser_sign_in'),
    ]

    operations = [
        migrations.AddField(
            model_name='agentdefinitionrevision',
            name='published_by_agent',
            field=models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='+', to='agents.agent'),
        ),
    ]

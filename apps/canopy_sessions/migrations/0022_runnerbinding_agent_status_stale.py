from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('canopy_sessions', '0021_runnerbinding_emdash_project'),
    ]

    operations = [
        migrations.AddField(
            model_name='runnerbinding',
            name='agent_status_stale',
            # Defaults False, which is exactly the pre-existing behaviour: trust the
            # engine flag outright. Only a runner new enough to dissent sets it.
            field=models.BooleanField(default=False),
        ),
    ]

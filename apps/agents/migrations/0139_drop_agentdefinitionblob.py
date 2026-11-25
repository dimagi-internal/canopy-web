# A stored agent's files are git objects now (0138 wrote them); the blob table goes.
from django.db import migrations


class Migration(migrations.Migration):

    dependencies = [
        ("agents", "0138_stored_definitions_to_git"),
    ]

    operations = [
        migrations.DeleteModel(
            name="AgentDefinitionBlob",
        ),
    ]

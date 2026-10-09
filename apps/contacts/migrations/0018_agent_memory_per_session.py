"""Agent memory per session (Jonathan, 2026-10-09): each feature gets a canopy-level
`available` and `default`, and a session may override the default.

The old switches become `*_available` (renamed, so who had them on keeps them on);
`*_default` is added. Data is set in 0019.
"""
from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):
    dependencies = [
        ("contacts", "0017_agent_memory_on_for_jonathan"),
        ("canopy_sessions", "0041_session_provenance"),
    ]

    operations = [
        migrations.RenameField("person", "hcp_record", "hcp_record_available"),
        migrations.RenameField("person", "hcp_use", "hcp_use_available"),
        migrations.AddField("person", "hcp_record_default", models.BooleanField(default=False)),
        migrations.AddField("person", "hcp_use_default", models.BooleanField(default=False)),
        migrations.CreateModel(
            name="SessionAgentMemory",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False,
                                           verbose_name="ID")),
                ("record", models.BooleanField(blank=True, null=True)),
                ("use", models.BooleanField(blank=True, null=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                ("person", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE,
                                             related_name="session_memory", to="contacts.person")),
                ("session", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE,
                                              related_name="agent_memory",
                                              to="canopy_sessions.session")),
            ],
            options={"db_table": "contact_session_agent_memory",
                     "constraints": [models.UniqueConstraint(fields=("session", "person"),
                                                             name="session_agent_memory_once")]},
        ),
    ]

from django.db import migrations


def declare_secrets_of_agents_that_declare_nothing(apps, schema_editor):
    """An agent whose secrets were all stored before #2093 (storing declares the name)
    holds values and declares none, so every scoped read of it gets nothing and every
    read raises an undeclared-ref alert — Trellis, set up on 2026-10-25 minutes before
    #2093 merged. Declare what such an agent stores, as #2093 would have.

    Only an agent that declares NOTHING: one that declares some names and stores
    another may hold a deliberate orphan (a declaration since removed), which stays
    undeclared and surfaced, as before."""
    Agent = apps.get_model("agents", "Agent")
    AgentCredential = apps.get_model("agents", "AgentCredential")
    for agent in Agent.objects.filter(credentials__isnull=False).distinct():
        if agent.runtime_secrets or agent.runtime_sources:
            continue
        names = sorted(set(AgentCredential.objects.filter(agent=agent)
                           .values_list("name", flat=True)))
        if names:
            Agent.objects.filter(pk=agent.pk).update(runtime_secrets=names)


class Migration(migrations.Migration):

    dependencies = [
        ('agents', '0093_retire_runtime_yaml_help_text'),
    ]

    operations = [
        migrations.RunPython(declare_secrets_of_agents_that_declare_nothing, migrations.RunPython.noop),
    ]

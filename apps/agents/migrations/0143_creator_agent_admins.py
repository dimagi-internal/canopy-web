# The agent whose own login created a stored agent administers it: revision 1 authored by
# an agent's login (`Agent.user`) makes that agent an admin of the agent it created. Rows are
# added, never removed, and only where none exists, so running it again changes nothing.
from django.db import migrations


def creators_administer(apps, schema_editor):
    Agent = apps.get_model("agents", "Agent")
    AgentAdmin = apps.get_model("agents", "AgentAdmin")
    AgentDefinitionRevision = apps.get_model("agents", "AgentDefinitionRevision")
    logins = dict(Agent.objects.exclude(user_id=None).values_list("user_id", "pk"))
    firsts = AgentDefinitionRevision.objects.filter(number=1).exclude(author_user_id=None)
    for agent_id, author_id in firsts.values_list("agent_id", "author_user_id"):
        creator = logins.get(author_id)
        if creator is None or creator == agent_id:
            continue
        AgentAdmin.objects.get_or_create(agent_id=agent_id, user_id=author_id)


class Migration(migrations.Migration):

    dependencies = [
        ("agents", "0142_definition_published_by_agent"),
    ]

    operations = [
        migrations.RunPython(creators_administer, migrations.RunPython.noop),
    ]

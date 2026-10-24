"""A check-in's agents become a many-to-many to Agent (design 2026-10-24, D6):
the JSON list of slugs is copied across, a slug that names no agent is dropped."""
from django.db import migrations, models


def copy_agents(apps, schema_editor):
    ProjectCheckin = apps.get_model("huddles", "ProjectCheckin")
    Agent = apps.get_model("agents", "Agent")
    for checkin in ProjectCheckin.objects.all():
        slugs = [s for s in (checkin.agents or []) if isinstance(s, str)]
        checkin.agent_members.set(Agent.objects.filter(slug__in=slugs))


class Migration(migrations.Migration):
    dependencies = [
        ("agents", "0085_workspace_tasks_constraints"),
        ("huddles", "0005_drop_work_groups"),
    ]

    operations = [
        migrations.AddField(
            model_name="projectcheckin",
            name="agent_members",
            field=models.ManyToManyField(blank=True, related_name="checkins", to="agents.agent"),
        ),
        migrations.RunPython(copy_agents, migrations.RunPython.noop),
        migrations.RemoveField(model_name="projectcheckin", name="agents"),
    ]

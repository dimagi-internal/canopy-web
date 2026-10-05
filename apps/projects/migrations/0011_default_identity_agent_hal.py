"""Default canopy, canopy-web, ace-web and connect-labs project-dispatch turns
(no agent_slug) to Hal's GitHub identity, so they stop being refused outright.

Before this, `apps.agents.delegations.turn_agent` returns None for a project
turn and `github_token_for_turn` raises — by design, there is no shared
fallback (runner/ec2/tests/test_github_per_turn.py:
test_a_refused_turn_runs_with_no_github_identity_at_all). That refusal is
correct for a project with no configured identity; these four are the
fleet/web repos Hal already has a GitHub delegation for.

Slugs are the canonical ones from `apps/projects/management/commands/
seed_projects.py` — fails closed (silently skips) for any slug or the `hal`
agent not existing yet in a given environment, same as `0009`'s workspace
backfill.
"""
from django.db import migrations

DEFAULT_IDENTITY_SLUGS = ["canopy", "canopy-web", "ace-web", "connect-labs"]
IDENTITY_AGENT_SLUG = "hal"


def seed(apps, schema_editor):
    Agent = apps.get_model("agents", "Agent")
    Project = apps.get_model("projects", "Project")
    hal = Agent.objects.filter(slug=IDENTITY_AGENT_SLUG).first()
    if hal is None:
        return
    Project.objects.filter(slug__in=DEFAULT_IDENTITY_SLUGS).update(default_identity_agent=hal)


def unseed(apps, schema_editor):
    Project = apps.get_model("projects", "Project")
    Project.objects.filter(slug__in=DEFAULT_IDENTITY_SLUGS).update(default_identity_agent=None)


class Migration(migrations.Migration):

    dependencies = [
        ('projects', '0010_project_default_identity_agent'),
    ]

    operations = [migrations.RunPython(seed, unseed)]

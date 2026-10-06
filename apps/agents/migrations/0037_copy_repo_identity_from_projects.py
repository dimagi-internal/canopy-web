"""Copy each workbench project's `default_identity_agent` into a RepoIdentity row.

The workbench project registry is being retired (its tables are dropped by
projects/0013, which depends on this). The one live thing it held was this
mapping: an agentless project turn on repo X runs as agent Y's GitHub identity
(projects/0011 set canopy, canopy-web, ace-web and connect-labs to hal). The
project slug IS the repo name `Turn.project` carries, so it copies across
unchanged.

Idempotent (update_or_create) so a re-run converges. Reverse is a no-op: the
projects rows still hold the mapping until projects/0013 drops them, and after
that there is nothing to copy back to.
"""
from django.db import migrations


def copy_forward(apps, schema_editor):
    Project = apps.get_model("projects", "Project")
    RepoIdentity = apps.get_model("agents", "RepoIdentity")
    rows = (
        Project.objects.exclude(default_identity_agent=None)
        .values_list("slug", "default_identity_agent_id")
    )
    for slug, agent_id in rows:
        RepoIdentity.objects.update_or_create(repo_slug=slug, defaults={"agent_id": agent_id})


class Migration(migrations.Migration):

    dependencies = [
        ("agents", "0036_repo_identity"),
        # The last migration that shapes the rows read here; projects/0013 (the
        # drop) depends on THIS migration, so the copy always runs first.
        ("projects", "0012_delete_insight_rows"),
    ]

    operations = [migrations.RunPython(copy_forward, migrations.RunPython.noop)]

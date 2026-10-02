"""Home every project that still has no workspace, before NULL stops meaning
"everyone".

`0007` backfilled the projects that existed then, and the API has assigned a
workspace on every create since — but `manage.py seed_projects` did not, so a
NULL row could still appear. Until this release a NULL project was visible to
ANY signed-in user; from it, a NULL project is visible to nobody. Moving the
stragglers into the org default keeps them where the people who could see them
yesterday (every Dimagi login) can still see them, and no wider.

If the org default does not exist there is no tenant to give them, and they
stay NULL — invisible, which is the fail-closed answer.
"""
from django.db import migrations

DEFAULT_WORKSPACE_SLUG = "dimagi"


def home(apps, schema_editor):
    Workspace = apps.get_model("workspaces", "Workspace")
    Project = apps.get_model("projects", "Project")
    if Workspace.objects.filter(slug=DEFAULT_WORKSPACE_SLUG).exists():
        Project.objects.filter(workspace__isnull=True).update(workspace_id=DEFAULT_WORKSPACE_SLUG)


class Migration(migrations.Migration):
    dependencies = [
        ("projects", "0008_project_created_by"),
        ("workspaces", "0010_workspace_parent"),
    ]
    operations = [migrations.RunPython(home, migrations.RunPython.noop)]

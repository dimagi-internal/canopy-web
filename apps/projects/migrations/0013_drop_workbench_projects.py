"""Drop the retired workbench Projects tables (2026-10-06).

Jonathan: "retire that in favor of the agent's project management and tasking
system, nothing needs to live outside that." What was still live was moved out
first, and this migration depends on both moves so it can never run before them:

- agents/0037 copied `Project.default_identity_agent` into `agents.RepoIdentity`
  (the repo-turn GitHub identity);
- shareouts/0008 replaced `Shareout.project` (the last FK INTO these tables)
  with a `project_slug` string.

What is dropped and not kept: every `ProjectContext` row (current_work /
next_step / summary / note — the last written 2026-04-13, no writer since) and
every `ProjectAction` row (skill-run tracking from the canopy plugin hook, whose
writer is removed in parallel). Irreversible in data: the reverse recreates the
empty tables only.
"""
from django.db import migrations


class Migration(migrations.Migration):

    dependencies = [
        ("projects", "0012_delete_insight_rows"),
        ("agents", "0037_copy_repo_identity_from_projects"),
        ("shareouts", "0008_shareout_project_slug"),
    ]

    operations = [
        migrations.DeleteModel(name="ProjectAction"),
        migrations.DeleteModel(name="ProjectContext"),
        migrations.DeleteModel(name="Project"),
    ]

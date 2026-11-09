"""The workbench Projects app — a migrations-only stub.

The agent project/task system (`apps.agents` `AgentProject` / `AgentTask`) holds
projects. The repo-turn GitHub identity lives in `agents.RepoIdentity` (agents/0037
copies it) and a shareout's project in `Shareout.project_slug` (shareouts/0008
copies it); `projects/0013` drops the three old tables (a registry of repos, their
context feed and skill-run log).

The app stays installed only so that migration chain stays whole: two other
apps' historical migrations depend on `projects` nodes (shareouts/0001 creates an
FK to `projects.Project`; workspaces/0008 orders after projects/0007), and a
fresh `migrate` must be able to replay them. It has no models, routes or code.
"""
from django.apps import AppConfig


class ProjectsConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "apps.projects"
    label = "projects"
    verbose_name = "Projects (retired — migrations only)"

from django.apps import AppConfig


class ProjectsConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "apps.projects"
    label = "projects"

    def ready(self) -> None:
        # projects (product) registers into agents (framework) rather than the
        # other way around — ARCHITECTURE.md's one-way arrow forbids
        # apps.agents importing apps.projects.
        from apps.agents import delegations

        from .identity import resolve_default_identity_agent_slug

        delegations.register_project_identity_resolver(resolve_default_identity_agent_slug)

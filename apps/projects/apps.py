from django.apps import AppConfig


class ProjectsConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "apps.projects"
    label = "projects"

    def ready(self) -> None:
        # Importing is what CONNECTS the receivers — a signals module nothing
        # imports is dead code that looks alive, which is precisely how
        # `apps/mcp/page_tools.py` shipped with ten passing tests and a feature
        # that did not exist. `test_page_invalidation` asserts against the
        # connected receiver rather than the function, so this line cannot be
        # removed silently.
        from . import signals  # noqa: F401  (page-invalidation receiver)

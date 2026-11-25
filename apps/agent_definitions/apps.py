from django.apps import AppConfig


class AgentDefinitionsConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "apps.agent_definitions"

    def ready(self) -> None:
        # The approval kind lives with the feature (approvals' own `kinds/` package
        # registers only what it loads itself), so it registers from here.
        from . import publish_kind  # noqa: F401, PLC0415 — needs the app registry ready

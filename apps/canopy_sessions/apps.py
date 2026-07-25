from django.apps import AppConfig


class CanopySessionsConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "apps.canopy_sessions"
    label = "canopy_sessions"

    def ready(self) -> None:
        from . import signals  # noqa: F401  (Message projection receiver)
        from . import autotitle  # noqa: F401  (auto-title receiver)

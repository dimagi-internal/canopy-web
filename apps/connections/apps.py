from django.apps import AppConfig


class ConnectionsConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "apps.connections"
    label = "connections"
    verbose_name = "Connections"

    def ready(self) -> None:
        from . import signals  # noqa: F401, PLC0415 - registers the receivers; models must be loaded first

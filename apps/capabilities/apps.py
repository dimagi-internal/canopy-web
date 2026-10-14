from django.apps import AppConfig


class CapabilitiesConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "apps.capabilities"
    label = "capabilities"

    def ready(self) -> None:
        # Releases a turn-bound lease when its turn ends (signals.py).
        from . import signals  # noqa: F401

from django.apps import AppConfig


class AdmissionConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "apps.admission"
    label = "admission"

    def ready(self) -> None:
        # Seeds the default rules into every NEW workspace (existing ones were
        # seeded by migration 0002).
        from . import signals  # noqa: F401

from django.apps import AppConfig


class LocalAppsConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "apps.local_apps"

    def ready(self) -> None:
        # approvals is framework and may not import a product app, so the kind
        # registers itself from here rather than from `approvals/kinds/`.
        from . import release_kind  # noqa: F401, PLC0415 — needs the app registry ready

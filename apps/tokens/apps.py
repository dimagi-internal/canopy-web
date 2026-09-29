from django.apps import AppConfig


class TokensConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "apps.tokens"
    label = "tokens"

    def ready(self):
        from . import signals  # noqa: F401 - connects the live-probe sweep

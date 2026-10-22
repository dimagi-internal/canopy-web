from django.apps import AppConfig


class AttentionConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "apps.attention"
    label = "attention"

    def ready(self) -> None:
        # Reconcile every alert source, expire stale alerts, notify new blocking
        # ones — off-request, fleet-wide once a minute.
        from apps.common import ticks

        from . import alerts

        ticks.register("alerts", alerts.tick, 60)

from django.apps import AppConfig

from apps.common import ticks


class AttentionConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "apps.attention"
    label = "attention"

    def ready(self) -> None:
        # Reconcile every alert source, expire stale alerts, notify new blocking
        # ones — off-request, fleet-wide once a minute.
        from . import alerts  # noqa: PLC0415 — alerts imports models, which the app registry is not ready for at module load

        ticks.register("alerts", alerts.tick, 60)

from django.apps import AppConfig


class RetentionConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "apps.retention"
    label = "retention"

    def ready(self) -> None:
        # Content past its retention rule; a no-op unless CANOPY_RETENTION_ENFORCE.
        from apps.common import ticks

        ticks.register("retention.sweep", _sweep, 60)


def _sweep() -> None:
    from .services import maybe_sweep

    maybe_sweep()

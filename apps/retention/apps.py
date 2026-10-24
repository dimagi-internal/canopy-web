import datetime as dt

from django.apps import AppConfig


class RetentionConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "apps.retention"
    label = "retention"

    def ready(self) -> None:
        # Content past its retention rule; a no-op unless CANOPY_RETENTION_ENFORCE.
        from apps.common import ticks

        ticks.register("retention.sweep", _sweep, 60)
        # The event log's own retention: rows untouched for 30 days, once a day.
        # Registered here, not in apps/events, whose AppConfig stays hook-free so
        # the log stays inert (tests/test_events_emits_nothing.py).
        ticks.register("events.prune", _prune_events, 24 * 60 * 60)


def _sweep() -> None:
    from .services import maybe_sweep

    maybe_sweep()


def _prune_events() -> None:
    from apps.events.services import prune  # noqa: PLC0415 — models: the app registry is not ready at module load

    prune(older_than=dt.timedelta(days=30))
